//! MCP servers and their tools — `core/mcp.py`'s `McpManager`, owned by the
//! edge. Change both.
//!
//! The configured servers (`config.rs`) are each asked for their tools once,
//! at start and on every reload; the listings are cached here, attributed to
//! their server. A call opens a fresh session to its server (`session.rs`),
//! as `langchain_mcp_adapters` does, so a stdio server only runs while it is
//! being asked something.
//!
//! An `always` server's tools are bound to the agent (`agent/turn.rs`); a
//! `lazy` one's are reached through `jarvis.mcp_call` (`callMcpTool`).
//! Python behind the edge has no MCP client of its own: it reads this
//! manager's state and calls through it (`internal.rs`,
//! `core/mcp.py:EdgeMcp`), and is told to re-read after every change.

pub mod config;
mod http;
mod session;
mod stdio;
mod ws;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use indexmap::IndexMap;
use serde_json::{Map, Value, json};
use sqlx::SqlitePool;

use crate::pyjson;
use config::{Connection, Servers};
use session::Session;

/// How long one server may take to list its tools. Python waits forever;
/// a first `npx -y` on old hardware can take a minute or two.
const LIST_TIMEOUT: Duration = Duration::from_secs(300);
/// `call_mcp_tool`'s default.
pub const CALL_TIMEOUT: f64 = 120.0;

/// One tool a server listed.
#[derive(Clone, Debug)]
pub struct Tool {
    pub name: String,
    pub description: String,
    /// The server's JSON Schema, as sent.
    pub input_schema: Value,
}

/// What the manager has loaded: the connections it used and each server's
/// tools. A server that failed to list has none; one never asked is absent.
#[derive(Default)]
pub struct Snapshot {
    pub connections: Servers,
    pub tools: IndexMap<String, Vec<Tool>>,
    pub default_mode: &'static str,
}

impl Snapshot {
    pub fn mode(&self, server: &str) -> &'static str {
        config::mode_for(self.connections.get(server), self.default_mode)
    }

    pub fn tools_for(&self, server: &str) -> &[Tool] {
        self.tools.get(server).map(Vec::as_slice).unwrap_or_default()
    }

    /// `get_bound_tools_sync`: the `always` servers' tools, with their server.
    pub fn bound(&self) -> Vec<(&str, &Tool)> {
        self.tools
            .iter()
            .filter(|(name, _)| self.mode(name) == config::ALWAYS)
            .flat_map(|(name, tools)| tools.iter().map(move |t| (name.as_str(), t)))
            .collect()
    }

    /// `_mcp_owner_map`: tool name → the server that lists it (the last one,
    /// if two do).
    pub fn owners(&self) -> HashMap<String, String> {
        let mut out = HashMap::new();
        for (server, tools) in &self.tools {
            for t in tools {
                out.insert(t.name.clone(), server.clone());
            }
        }
        out
    }

    /// `find_tool`, its misses worded as Python's `ValueError`s.
    pub fn find(&self, server: &str, tool: &str) -> Result<&Tool, String> {
        if !self.connections.contains_key(server) {
            let names: Vec<&str> = self.connections.keys().map(String::as_str).collect();
            let configured = if names.is_empty() { "(none)".to_string() } else { names.join(", ") };
            return Err(format!("Unknown MCP server {}. Configured: {configured}", pyjson::repr_str(server)));
        }
        let Some(tools) = self.tools.get(server) else {
            return Err(format!("MCP server {} is not loaded — reload MCP servers and retry", pyjson::repr_str(server)));
        };
        if let Some(t) = tools.iter().find(|t| t.name == tool) {
            return Ok(t);
        }
        let names: Vec<&str> = tools.iter().map(|t| t.name.as_str()).collect();
        let available = if names.is_empty() { "(none)".to_string() } else { names.join(", ") };
        Err(format!("MCP server {} has no tool {}. Available: {available}", pyjson::repr_str(server), pyjson::repr_str(tool)))
    }
}

/// A tool call's outcome: the content as LangChain blocks (what the
/// adapter's `ToolMessage` carries), the structured content, and whether
/// the server said it failed.
pub struct CallResult {
    pub blocks: Vec<Value>,
    pub artifact: Option<Value>,
    pub is_error: bool,
}

impl CallResult {
    /// `_content_to_text`: text blocks as their text, anything else as JSON.
    pub fn text(&self) -> String {
        self.blocks
            .iter()
            .map(|b| match (b.get("type"), b.get("text")) {
                (Some(t), Some(Value::String(s))) if t == "text" => s.clone(),
                _ => pyjson::dumps(b),
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// `ensure_id(None)`.
fn block_id() -> String {
    format!("lc_{}", uuid::Uuid::new_v4())
}

/// `_convert_mcp_content_to_lc_block`.
fn lc_block(content: &Value) -> Result<Value, String> {
    let s = |key: &str| content.get(key).filter(|v| !v.is_null()).map(pyjson::py_str);
    let media = |kind: &str, url: Option<String>, base64: Option<String>, mime: Option<String>| {
        let mut b = Map::new();
        b.insert("type".into(), kind.into());
        b.insert("id".into(), block_id().into());
        if let Some(u) = url {
            b.insert("url".into(), u.into());
        }
        if let Some(d) = base64 {
            b.insert("base64".into(), d.into());
        }
        if let Some(m) = mime {
            b.insert("mime_type".into(), m.into());
        }
        Value::Object(b)
    };
    let text = |t: String| json!({"type": "text", "text": t, "id": block_id()});
    let mime = s("mimeType").filter(|m| !m.is_empty());
    match content.get("type").and_then(Value::as_str) {
        Some("text") => Ok(text(s("text").unwrap_or_default())),
        Some("image") => Ok(media("image", None, s("data"), mime)),
        Some("audio") => Err(format!(
            "AudioContent conversion to LangChain content blocks is not yet supported. Received audio with mime type: {}",
            mime.unwrap_or_default()
        )),
        Some("resource_link") => {
            let kind = if mime.as_deref().is_some_and(|m| m.starts_with("image/")) { "image" } else { "file" };
            Ok(media(kind, s("uri"), None, mime))
        }
        Some("resource") => {
            let resource = content.get("resource").cloned().unwrap_or_default();
            if let Some(t) = resource.get("text").filter(|v| !v.is_null()) {
                return Ok(text(pyjson::py_str(t)));
            }
            if let Some(blob) = resource.get("blob").filter(|v| !v.is_null()) {
                let mime = resource.get("mimeType").filter(|v| !v.is_null()).map(pyjson::py_str).filter(|m| !m.is_empty());
                let kind = if mime.as_deref().is_some_and(|m| m.starts_with("image/")) { "image" } else { "file" };
                return Ok(media(kind, None, Some(pyjson::py_str(blob)), mime));
            }
            Err("Unknown embedded resource type".into())
        }
        other => Err(format!("Unknown MCP content type: {}", other.unwrap_or("?"))),
    }
}

/// `_convert_call_tool_result` + the adapter's error handler.
fn convert(result: &Value) -> Result<CallResult, String> {
    let blocks = match result.get("content") {
        Some(Value::Array(items)) => items.iter().map(lc_block).collect::<Result<Vec<_>, _>>()?,
        _ => vec![],
    };
    if result.get("isError").is_some_and(pyjson::truthy) {
        let blocks = if blocks.is_empty() {
            vec![json!({"type": "text", "text": "MCP tool returned an error with empty content.", "id": block_id()})]
        } else {
            blocks
        };
        return Ok(CallResult { blocks, artifact: None, is_error: true });
    }
    let artifact = result.get("structuredContent").filter(|v| !v.is_null()).map(|sc| json!({"structured_content": sc}));
    Ok(CallResult { blocks, artifact, is_error: false })
}

/// The tools of one `tools/list`, as the `Tool` model validates them: a
/// listing with one malformed tool fails whole.
fn parse_tools(listed: Vec<Value>) -> Result<Vec<Tool>, String> {
    listed
        .into_iter()
        .map(|t| {
            let name = t.get("name").and_then(Value::as_str).ok_or("1 validation error for Tool: name")?;
            let schema = t.get("inputSchema").filter(|s| s.is_object()).ok_or("1 validation error for Tool: inputSchema")?;
            Ok(Tool {
                name: name.to_string(),
                description: t.get("description").and_then(Value::as_str).unwrap_or_default().to_string(),
                input_schema: schema.clone(),
            })
        })
        .collect()
}

async fn list(cfg: &Connection) -> Result<Vec<Tool>, String> {
    let cfg = config::strip_jarvis_keys(cfg);
    let mut session = Session::open(&cfg).await.map_err(|e| e.0)?;
    let listed = session.list_tools().await;
    session.close().await;
    parse_tools(listed.map_err(|e| e.0)?)
}

async fn call(cfg: &Connection, tool: &str, args: &Value) -> Result<CallResult, String> {
    let cfg = config::strip_jarvis_keys(cfg);
    let mut session = Session::open(&cfg).await.map_err(|e| e.0)?;
    let result = session.call_tool(tool, args).await;
    session.close().await;
    convert(&result.map_err(|e| e.0)?)
}

/// `f"{x:g}"` for the timeouts people set: whole seconds without a point.
fn format_g(x: f64) -> String {
    if x.fract() == 0.0 && x.abs() < 1e16 {
        format!("{}", x as i64)
    } else {
        let s = format!("{x:.6}");
        let s = s.trim_end_matches('0').trim_end_matches('.');
        s.to_string()
    }
}

pub struct Mcp {
    pool: SqlitePool,
    app_dir: PathBuf,
    current: RwLock<Option<Arc<Snapshot>>>,
    /// One load at a time.
    loading: tokio::sync::Mutex<()>,
}

impl Mcp {
    pub fn new(pool: SqlitePool, app_dir: PathBuf) -> Arc<Self> {
        Arc::new(Mcp { pool, app_dir, current: RwLock::new(None), loading: tokio::sync::Mutex::new(()) })
    }

    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }

    /// The configured servers as `mcpServers` merges them right now.
    pub async fn configured(&self) -> Servers {
        config::merged(&self.pool, &self.app_dir).await
    }

    pub async fn default_mode(&self) -> &'static str {
        config::default_mode(&self.pool).await
    }

    /// The loaded state, loading it first if nothing has been yet.
    pub async fn snapshot(&self) -> Arc<Snapshot> {
        if let Some(s) = self.current.read().expect("mcp state").clone() {
            return s;
        }
        let _guard = self.loading.lock().await;
        if let Some(s) = self.current.read().expect("mcp state").clone() {
            return s;
        }
        let s = self.load().await;
        *self.current.write().expect("mcp state") = Some(s.clone());
        s
    }

    /// `_initialize_locked`: every configured server asked for its tools,
    /// concurrently; one that can't answer has none.
    async fn load(&self) -> Arc<Snapshot> {
        let connections = self.configured().await;
        let default_mode = self.default_mode().await;
        let listings = futures_util::future::join_all(connections.iter().map(|(name, cfg)| async move {
            let result = match tokio::time::timeout(LIST_TIMEOUT, list(cfg)).await {
                Ok(r) => r,
                Err(_) => Err(format!("timed out after {}s", LIST_TIMEOUT.as_secs())),
            };
            (name.clone(), result)
        }))
        .await;
        let mut tools = IndexMap::new();
        for (name, result) in listings {
            match result {
                Ok(t) => {
                    tools.insert(name, t);
                }
                Err(e) => {
                    tracing::warn!("MCP server '{name}' failed to load tools: {e}");
                    tools.insert(name, vec![]);
                }
            }
        }
        let snapshot = Snapshot { connections, tools, default_mode };
        if !snapshot.connections.is_empty() {
            let counts: Vec<String> =
                snapshot.tools.iter().map(|(n, t)| format!("{n}:{}/{}", t.len(), snapshot.mode(n))).collect();
            tracing::info!(
                "MCP tools loaded: {} tools from {} servers ({})",
                snapshot.tools.values().map(Vec::len).sum::<usize>(),
                snapshot.connections.len(),
                counts.join(", ")
            );
        }
        Arc::new(snapshot)
    }

    /// `reload`: the merged config re-read and every server asked again.
    /// Readers see the old state until the new one is complete.
    pub async fn reload(&self) -> Arc<Snapshot> {
        let s = {
            let _guard = self.loading.lock().await;
            let s = self.load().await;
            *self.current.write().expect("mcp state") = Some(s.clone());
            s
        };
        s
    }

    /// `set_load_mode`: one server's mode flipped in place — the tools are
    /// already loaded, only what is bound changes.
    pub async fn set_mode(&self, server: &str, mode: &str) -> Result<Arc<Snapshot>, String> {
        let current = self.snapshot().await;
        let s = {
            let _guard = self.loading.lock().await;
            let current = self.current.read().expect("mcp state").clone().unwrap_or(current);
            let Some(cfg) = current.connections.get(server) else {
                return Err(format!("MCP server {} is not configured", pyjson::repr_str(server)));
            };
            let mut connections = current.connections.clone();
            connections.insert(server.to_string(), config::with_mode(cfg, mode));
            let s = Arc::new(Snapshot { connections, tools: current.tools.clone(), default_mode: current.default_mode });
            *self.current.write().expect("mcp state") = Some(s.clone());
            s
        };
        Ok(s)
    }

    /// `call_mcp_tool`: one tool by (server, tool), within `timeout` seconds
    /// (a timeout is an error result, not a failure).
    pub async fn call(&self, server: &str, tool: &str, args: &Value, timeout: Option<f64>) -> Result<CallResult, String> {
        let snapshot = self.snapshot().await;
        let found = snapshot.find(server, tool)?;
        let cfg = snapshot.connections.get(server).expect("find checked the server");
        let ran = call(cfg, &found.name, args);
        match timeout {
            None => ran.await,
            Some(secs) => match tokio::time::timeout(Duration::from_secs_f64(secs.max(0.0)), ran).await {
                Ok(r) => r,
                Err(_) => Ok(CallResult {
                    blocks: vec![json!({"type": "text", "text": format!("MCP tool {server}.{tool} timed out after {}s", format_g(secs))})],
                    artifact: None,
                    is_error: true,
                }),
            },
        }
    }
}

// ── binding ─────────────────────────────────────────────────────────────────

/// `_retrieve_ref`: a `#/a/b` path into the schema.
fn retrieve_ref(path: &str, schema: &Value) -> Result<Value, String> {
    let mut parts = path.split('/');
    if parts.next() != Some("#") {
        return Err("ref paths are expected to be URI fragments, meaning they should start with #.".into());
    }
    let mut out = schema;
    for part in parts {
        out = match out {
            Value::Object(m) if m.contains_key(part) => &m[part],
            Value::Array(items) if part.bytes().all(|b| b.is_ascii_digit()) && !part.is_empty() => {
                part.parse::<usize>().ok().and_then(|i| items.get(i)).ok_or_else(|| format!("Reference '{path}' not found."))?
            }
            _ => return Err(format!("Reference '{path}' not found.")),
        };
    }
    Ok(out.clone())
}

/// `_dereference_refs_helper` with `skip_keys=["$defs"]`.
fn deref(obj: &Value, full: &Value, seen: &mut Vec<String>) -> Result<Value, String> {
    match obj {
        Value::Object(map) if map.contains_key("$ref") => {
            let path = pyjson::py_str(&map["$ref"]);
            let extra: Map<String, Value> = map.iter().filter(|(k, _)| *k != "$ref").map(|(k, v)| (k.clone(), v.clone())).collect();
            if seen.contains(&path) {
                return deref_props(&extra, full, seen);
            }
            seen.push(path.clone());
            let resolved = deref(&retrieve_ref(&path, full)?, full, seen);
            seen.retain(|p| p != &path);
            let resolved = resolved?;
            if extra.is_empty() {
                return Ok(resolved);
            }
            let mut merged = match resolved {
                Value::Object(m) => m,
                _ => Map::new(),
            };
            let Value::Object(extra) = deref_props(&extra, full, seen)? else { unreachable!() };
            merged.extend(extra);
            Ok(Value::Object(merged))
        }
        Value::Object(map) => deref_props(map, full, seen),
        Value::Array(items) => Ok(Value::Array(items.iter().map(|v| deref(v, full, seen)).collect::<Result<_, _>>()?)),
        other => Ok(other.clone()),
    }
}

fn deref_props(map: &Map<String, Value>, full: &Value, seen: &mut Vec<String>) -> Result<Value, String> {
    let mut out = Map::new();
    for (k, v) in map {
        let v = if k == "$defs" || !(v.is_object() || v.is_array()) { v.clone() } else { deref(v, full, seen)? };
        out.insert(k.clone(), v);
    }
    Ok(Value::Object(out))
}

/// `_rm_titles`: every `title` dropped, except a property named "title".
fn rm_titles(map: &Map<String, Value>, prev: &str) -> Map<String, Value> {
    let mut out = Map::new();
    for (k, v) in map {
        match v {
            Value::Object(inner) if k == "title" && prev == "properties" => {
                out.insert(k.clone(), Value::Object(rm_titles(inner, k)));
            }
            _ if k == "title" => {}
            Value::Object(inner) => {
                out.insert(k.clone(), Value::Object(rm_titles(inner, k)));
            }
            other => {
                out.insert(k.clone(), other.clone());
            }
        }
    }
    out
}

/// `convert_to_openai_tool` for an adapter tool (a dict `args_schema`):
/// refs inlined, `$defs` and every title dropped, the description the
/// tool's own or else the schema's.
pub fn llm_tool(tool: &Tool) -> Result<crate::llm::Tool, String> {
    let mut schema = tool.input_schema.clone();
    if !tool.description.is_empty() {
        schema["description"] = tool.description.clone().into();
    }
    let Value::Object(mut schema) = deref(&schema, &schema.clone(), &mut vec![])? else {
        return Err("the input schema isn't an object".into());
    };
    schema.shift_remove("definitions");
    schema.shift_remove("$defs");
    schema.shift_remove("title");
    let description = schema.shift_remove("description").map(|d| pyjson::py_str(&d)).unwrap_or_default();
    Ok(crate::llm::Tool { name: tool.name.clone(), description, parameters: Value::Object(rm_titles(&schema, "")) })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tool(schema: Value, description: &str) -> Tool {
        Tool { name: "t".into(), description: description.into(), input_schema: schema }
    }

    #[test]
    fn openai_tool_matches_langchain() {
        // What convert_to_openai_tool gave for FastMCP's `add`.
        let t = llm_tool(&tool(
            json!({"properties": {"a": {"title": "A", "type": "integer"}, "b": {"title": "B", "type": "integer"}},
                   "required": ["a", "b"], "title": "addArguments", "type": "object"}),
            "Add two integers.",
        ))
        .unwrap();
        assert_eq!(t.description, "Add two integers.");
        assert_eq!(
            t.parameters,
            json!({"properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"], "type": "object"})
        );
        // Refs inlined, $defs gone; a property called "title" kept; a cycle broken.
        let t = llm_tool(&tool(
            json!({"type": "object", "description": "own", "properties": {
                "title": {"type": "string", "title": "Title"},
                "node": {"$ref": "#/$defs/Node", "description": "n"},
                "list": {"anyOf": [{"title": "kept in lists", "type": "null"}]}},
               "$defs": {"Node": {"type": "object", "title": "Node", "properties": {"next": {"$ref": "#/$defs/Node"}}}}}),
            "",
        ))
        .unwrap();
        assert_eq!(t.description, "own");
        assert_eq!(
            t.parameters,
            json!({"type": "object", "properties": {
                "title": {"type": "string"},
                "node": {"type": "object", "properties": {"next": {}}, "description": "n"},
                "list": {"anyOf": [{"title": "kept in lists", "type": "null"}]}}})
        );
        assert!(llm_tool(&tool(json!({"properties": {"x": {"$ref": "#/nope"}}}), "")).is_err());
    }

    #[test]
    fn results_convert_like_the_adapter() {
        let r = convert(&json!({"content": [{"type": "text", "text": "5"}], "structuredContent": {"result": 5}, "isError": false})).unwrap();
        assert!(!r.is_error && r.text() == "5");
        assert_eq!(r.artifact, Some(json!({"structured_content": {"result": 5}})));
        assert!(r.blocks[0]["id"].as_str().unwrap().starts_with("lc_"));
        let r = convert(&json!({"content": [], "isError": true})).unwrap();
        assert!(r.is_error && r.text() == "MCP tool returned an error with empty content." && r.artifact.is_none());
        let r = convert(&json!({"content": [
            {"type": "image", "data": "AA==", "mimeType": "image/png"},
            {"type": "resource", "resource": {"uri": "u", "text": "inside"}},
            {"type": "resource_link", "uri": "file:///a.pdf", "name": "a", "mimeType": "application/pdf"}]}))
        .unwrap();
        assert_eq!(r.blocks[0]["type"], "image");
        assert_eq!(r.blocks[0]["base64"], "AA==");
        assert_eq!(r.blocks[1]["text"], "inside");
        assert_eq!((r.blocks[2]["type"].as_str(), r.blocks[2]["url"].as_str()), (Some("file"), Some("file:///a.pdf")));
        assert!(r.text().starts_with("{\"type\": \"image\", \"id\": \"lc_"));
        assert!(convert(&json!({"content": [{"type": "audio", "data": "", "mimeType": "audio/wav"}]})).is_err());
    }

    #[test]
    fn misses_are_worded_like_find_tool() {
        let mut s = Snapshot { default_mode: config::ALWAYS, ..Default::default() };
        assert_eq!(s.find("x", "t").unwrap_err(), "Unknown MCP server 'x'. Configured: (none)");
        s.connections.insert("x".into(), Map::new());
        assert_eq!(s.find("x", "t").unwrap_err(), "MCP server 'x' is not loaded — reload MCP servers and retry");
        s.tools.insert("x".into(), vec![tool(json!({}), "")]);
        assert_eq!(s.find("x", "u").unwrap_err(), "MCP server 'x' has no tool 'u'. Available: t");
        assert_eq!(format_g(120.0), "120");
        assert_eq!(format_g(1.5), "1.5");
    }
}
