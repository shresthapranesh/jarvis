//! One MCP session: a transport, the `initialize` handshake, and the three
//! requests jarvis makes — `tools/list`, `tools/call`, and answering a
//! server's `ping`. What `mcp.ClientSession` and `create_session`
//! (`langchain_mcp_adapters/sessions.py`) do for each tool listing and call.
//!
//! A session is opened per listing and per call, as the adapter does it, so
//! a stdio server runs only while it is being asked something — nothing
//! stays resident between calls.

use serde_json::{Value, json};

use super::config::Connection;
use super::{http, stdio, ws};
use crate::pyjson;

/// `types.LATEST_PROTOCOL_VERSION` in the `mcp` package Python uses.
const PROTOCOL_VERSION: &str = "2025-11-25";
/// `SUPPORTED_PROTOCOL_VERSIONS`.
const SUPPORTED: [&str; 4] = ["2024-11-05", "2025-03-26", "2025-06-18", PROTOCOL_VERSION];
/// `_list_all_tools`' cap on pages.
const MAX_PAGES: usize = 1000;

/// A failed session or request, as Python's exception would say it: a
/// JSON-RPC error is `McpError(message)`, anything else the transport's.
#[derive(Debug)]
pub struct Error(pub String);

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl From<String> for Error {
    fn from(s: String) -> Self {
        Error(s)
    }
}

/// A message from the server, or why none will come.
pub enum Incoming {
    Message(Value),
    Closed(String),
}

pub enum Conn {
    Stdio(stdio::Stdio),
    Http(http::Http),
    Sse(http::Sse),
    Ws(ws::Ws),
}

impl Conn {
    async fn send(&mut self, msg: &Value) -> Result<(), String> {
        match self {
            Conn::Stdio(c) => c.send(msg).await,
            Conn::Http(c) => c.send(msg).await,
            Conn::Sse(c) => c.send(msg).await,
            Conn::Ws(c) => c.send(msg).await,
        }
    }

    async fn recv(&mut self) -> Incoming {
        match self {
            Conn::Stdio(c) => c.recv().await,
            Conn::Http(c) => c.recv(),
            Conn::Sse(c) => c.recv().await,
            Conn::Ws(c) => c.recv().await,
        }
    }

    async fn close(self) {
        match self {
            Conn::Stdio(c) => c.close().await,
            Conn::Http(c) => c.close().await,
            Conn::Sse(c) => c.close(),
            Conn::Ws(c) => c.close().await,
        }
    }
}

/// The keys each transport takes, as its `_create_*_session` signature
/// lists them; anything else is the `TypeError` Python would raise.
fn check_keys(transport: &str, func: &str, params: &Connection) -> Result<(), String> {
    let known: &[&str] = match transport {
        "stdio" => &["command", "args", "env", "cwd", "encoding", "encoding_error_handler", "session_kwargs"],
        "sse" => &["url", "headers", "timeout", "sse_read_timeout", "session_kwargs", "httpx_client_factory", "auth"],
        "websocket" => &["url", "session_kwargs"],
        _ => &["url", "headers", "timeout", "sse_read_timeout", "terminate_on_close", "session_kwargs", "httpx_client_factory", "auth"],
    };
    if let Some(key) = params.keys().find(|k| !known.contains(&k.as_str())) {
        return Err(format!("{func}() got an unexpected keyword argument '{key}'"));
    }
    // Python objects a JSON config can't spell.
    for key in ["session_kwargs", "httpx_client_factory", "auth"] {
        if params.get(key).is_some_and(|v| !v.is_null() && v != &json!({})) {
            return Err(format!("{key} is not supported in a JSON MCP config"));
        }
    }
    Ok(())
}

pub struct Session {
    conn: Conn,
    /// `ClientSession._request_id`: the next request's id.
    next_id: i64,
}

impl Session {
    /// `create_session` + `initialize()`: the transport the config names,
    /// handshake done.
    pub async fn open(cfg: &Connection) -> Result<Session, Error> {
        let Some(transport) = cfg.get("transport") else {
            return Err(Error(
                "Configuration error: Missing 'transport' key in server configuration. Each server must include \
                 'transport' with one of: 'stdio', 'sse', 'websocket', 'http'. Please refer to the \
                 langchain-mcp-adapters documentation for more details."
                    .into(),
            ));
        };
        let transport = pyjson::py_str(transport);
        let params: Connection = cfg.iter().filter(|(k, _)| *k != "transport").map(|(k, v)| (k.clone(), v.clone())).collect();
        let conn = match transport.as_str() {
            "sse" => {
                if !params.contains_key("url") {
                    return Err(Error("'url' parameter is required for SSE connection".into()));
                }
                check_keys("sse", "_create_sse_session", &params)?;
                Conn::Sse(http::Sse::open(&params).await?)
            }
            "streamable_http" | "streamable-http" | "http" => {
                if !params.contains_key("url") {
                    return Err(Error("'url' parameter is required for Streamable HTTP connection".into()));
                }
                check_keys("http", "_create_streamable_http_session", &params)?;
                Conn::Http(http::Http::open(&params)?)
            }
            "stdio" => {
                if !params.contains_key("command") {
                    return Err(Error("'command' parameter is required for stdio connection".into()));
                }
                if !params.contains_key("args") {
                    return Err(Error("'args' parameter is required for stdio connection".into()));
                }
                check_keys("stdio", "_create_stdio_session", &params)?;
                Conn::Stdio(stdio::Stdio::open(&params)?)
            }
            "websocket" => {
                if !params.contains_key("url") {
                    return Err(Error("'url' parameter is required for Websocket connection".into()));
                }
                check_keys("websocket", "_create_websocket_session", &params)?;
                Conn::Ws(ws::Ws::open(&params).await?)
            }
            other => {
                return Err(Error(format!("Unsupported transport: {other}. Must be one of: 'stdio', 'sse', 'websocket', 'http'")));
            }
        };
        let mut session = Session { conn, next_id: 0 };
        match session.initialize().await {
            Ok(()) => Ok(session),
            Err(e) => {
                session.close().await;
                Err(e)
            }
        }
    }

    async fn initialize(&mut self) -> Result<(), Error> {
        let result = self
            .request(
                "initialize",
                Some(json!({
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcp", "version": "0.1.0"},
                })),
            )
            .await?;
        let version = result.get("protocolVersion").map(pyjson::py_str).unwrap_or_default();
        if !SUPPORTED.contains(&version.as_str()) {
            return Err(Error(format!("Unsupported protocol version from the server: {version}")));
        }
        if let Conn::Http(h) = &mut self.conn {
            h.protocol_version = Some(version);
        }
        self.conn.send(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"})).await?;
        Ok(())
    }

    /// One request and its answer. A server's own requests on the way are
    /// answered (`ping`) or refused; its notifications are dropped.
    async fn request(&mut self, method: &str, params: Option<Value>) -> Result<Value, Error> {
        let id = self.next_id;
        self.next_id += 1;
        let mut msg = json!({"jsonrpc": "2.0", "id": id, "method": method});
        if let Some(p) = params {
            msg["params"] = p;
        }
        self.conn.send(&msg).await?;
        loop {
            let msg = match self.conn.recv().await {
                Incoming::Message(m) => m,
                Incoming::Closed(why) => return Err(Error(why)),
            };
            if let Some(m) = msg.get("method") {
                if let Some(their_id) = msg.get("id") {
                    let reply = if m == "ping" {
                        json!({"jsonrpc": "2.0", "id": their_id, "result": {}})
                    } else {
                        json!({"jsonrpc": "2.0", "id": their_id, "error": {"code": -32601, "message": "Method not found"}})
                    };
                    self.conn.send(&reply).await?;
                }
                continue;
            }
            let ours = match msg.get("id") {
                Some(Value::Number(n)) => n.as_i64() == Some(id),
                Some(Value::String(s)) => s.parse::<i64>().ok() == Some(id),
                _ => false,
            };
            if !ours {
                continue;
            }
            if let Some(err) = msg.get("error") {
                return Err(Error(err.get("message").map(pyjson::py_str).unwrap_or_default()));
            }
            return Ok(msg.get("result").cloned().unwrap_or(Value::Null));
        }
    }

    /// `_list_all_tools`: every page of `tools/list`.
    pub async fn list_tools(&mut self) -> Result<Vec<Value>, Error> {
        let mut tools = vec![];
        let mut cursor: Option<String> = None;
        for _ in 0..MAX_PAGES {
            let params = cursor.as_ref().map(|c| json!({"cursor": c}));
            let page = self.request("tools/list", params).await?;
            if let Some(Value::Array(items)) = page.get("tools") {
                tools.extend(items.iter().cloned());
            }
            // Pagination is over when the cursor is absent or empty.
            match page.get("nextCursor") {
                Some(Value::String(c)) if !c.is_empty() => cursor = Some(c.clone()),
                _ => return Ok(tools),
            }
        }
        Err(Error("Reached max of 1000 iterations while listing tools.".into()))
    }

    /// `tools/call`, its `CallToolResult` as the server sent it.
    pub async fn call_tool(&mut self, name: &str, args: &Value) -> Result<Value, Error> {
        self.request("tools/call", Some(json!({"name": name, "arguments": args}))).await
    }

    /// The transport's shutdown: stdin closed and the server waited for, an
    /// HTTP session ended.
    pub async fn close(self) {
        self.conn.close().await;
    }
}
