//! The MCP API — `server/graphql/types/mcp.py`, `queries/mcp.py` and
//! `mutations/mcp.py`, over the edge's manager (`crate::mcp`). Change both.
//!
//! An agent's call passes the per-tool policy (Settings → Tools), then the
//! blanket `call_mcp_tool` action when an operator gates it
//! (`approval::gate_action`).

use std::sync::Arc;

use async_graphql::{Context, Object, Result, SimpleObject};
use serde_json::{Value, json};
use sqlx::SqlitePool;

use super::router::Caller;
use super::settings::upsert;
use super::{EdgeData, RequestFrom};
use crate::mcp::config::{self, Connection};
use crate::mcp::{Mcp, Snapshot};
use crate::pyjson;
use crate::runs::Registry;

/// One tool exposed by a connected MCP server.
#[derive(SimpleObject)]
pub struct McpTool {
    name: String,
    server: String,
    description: String,
    /// JSON string — only worth selecting when you are about to call the tool.
    input_schema: String,
}

/// Outcome of one `callMcpTool` invocation.
#[derive(SimpleObject)]
pub struct McpToolResult {
    content: String,
    is_error: bool,
}

#[derive(SimpleObject)]
pub struct McpServer {
    name: String,
    config: String,
    transport: String,
    command: Option<String>,
    url: Option<String>,
    tool_count: i32,
    enabled: bool,
    load_mode: String,
    tools: Vec<String>,
}

impl McpServer {
    /// `McpServer.from_entry`.
    fn from_entry(name: &str, cfg: &Connection, tools: Vec<String>, default_mode: &'static str) -> Self {
        let transport = cfg
            .get("transport")
            .map(pyjson::py_str)
            .unwrap_or_else(|| if cfg.contains_key("command") { "stdio" } else { "http" }.into());
        let command = match cfg.get("command") {
            Some(Value::Array(parts)) => Some(parts.iter().map(pyjson::py_str).collect::<Vec<_>>().join(" ")),
            Some(c) if pyjson::truthy(c) => Some(pyjson::py_str(c)),
            _ => None,
        };
        McpServer {
            name: name.into(),
            config: pyjson::dumps(&Value::Object(config::redact(cfg))),
            transport,
            command,
            url: cfg.get("url").filter(|u| pyjson::truthy(u)).map(pyjson::py_str),
            tool_count: tools.len() as i32,
            enabled: true,
            load_mode: config::mode_for(Some(cfg), default_mode).into(),
            tools,
        }
    }

    /// `_server_type`: the manager's tools for it.
    fn from_snapshot(name: &str, cfg: &Connection, s: &Snapshot) -> Self {
        let tools = s.tools_for(name).iter().map(|t| t.name.clone()).collect();
        Self::from_entry(name, cfg, tools, s.default_mode)
    }
}

fn tool_type(server: &str, t: &crate::mcp::Tool) -> McpTool {
    McpTool {
        name: t.name.clone(),
        server: server.into(),
        description: t.description.clone(),
        input_schema: pyjson::dumps(&t.input_schema),
    }
}

/// A JSON argument, refused before anything is written when it doesn't parse.
fn parse_json(raw: &str, what: &str) -> Result<Value> {
    serde_json::from_str(raw).map_err(|e| format!("{what} is not valid JSON: {e}").into())
}

pub(crate) async fn write_setting(pool: &SqlitePool, key: &str, value: &str) -> Result<()> {
    let mut tx = crate::db::write_tx(pool).await?;
    upsert(&mut tx, key, value).await?;
    tx.commit().await?;
    Ok(())
}

/// `{name: cfg}`, the shape `_normalize_servers` is handed one server in.
fn single(name: &str, cfg: &Value) -> Value {
    let mut m = serde_json::Map::new();
    m.insert(name.into(), cfg.clone());
    Value::Object(m)
}

/// `add_mcp_server_to_db`: the server upserted into the `mcp.servers` setting.
pub(crate) async fn add_to_db(pool: &SqlitePool, name: &str, cfg: &Value) -> Result<()> {
    let mut servers = config::from_db(pool).await;
    let normalized = config::normalize(&single(name, cfg));
    if normalized.is_empty() {
        return Err(format!("invalid MCP server config for {name}").into());
    }
    servers.extend(normalized);
    write_setting(pool, config::SERVERS_KEY, &pyjson::dumps(&serde_json::to_value(&servers)?)).await
}

/// `addMcpServer` and `updateMcpServer`, which are the same upsert.
async fn upsert_server(ctx: &Context<'_>, name: &str, config_json: &str, not_object: &str) -> Result<McpServer> {
    let mut raw = parse_json(config_json, "config_json")?;
    if !raw.is_object() {
        return Err(not_object.to_string().into());
    }
    let mcp = ctx.data::<EdgeData>()?.mcp.clone();
    config::unmask(&mut raw, mcp.configured().await.get(name))?;
    let normalized = config::normalize(&single(name, &raw));
    let Some(fallback) = normalized.get(name).cloned() else {
        return Err(format!("invalid config for server {}", pyjson::repr_str(name)).into());
    };
    add_to_db(mcp.pool(), name, &raw).await?;
    let s = mcp.reload().await;
    let cfg = s.connections.get(name).cloned().unwrap_or(fallback);
    Ok(McpServer::from_snapshot(name, &cfg, &s))
}

/// The policy a human set for one tool key (`tools.policy`).
async fn policy(pool: &SqlitePool, key: &str) -> Result<(bool, bool)> {
    let raw = crate::catalog::setting(pool, "tools.policy").await?;
    let entry = match raw.as_deref().map(serde_json::from_str::<Value>) {
        Some(Ok(Value::Object(map))) => map.get(key).cloned(),
        _ => None,
    };
    Ok(match entry {
        Some(Value::Object(e)) => (e.get("enabled").is_none_or(pyjson::truthy), e.get("approval").is_some_and(pyjson::truthy)),
        _ => (true, false),
    })
}

#[derive(Default)]
pub struct McpQuery;

#[Object]
impl McpQuery {
    async fn mcp_servers(&self, ctx: &Context<'_>) -> Result<Vec<McpServer>> {
        let mcp = &ctx.data::<EdgeData>()?.mcp;
        let default_mode = mcp.default_mode().await;
        let merged = mcp.configured().await;
        let s = mcp.snapshot().await;
        let tools_of = |name: &str| -> Vec<String> { s.tools_for(name).iter().map(|t| t.name.clone()).collect() };
        let mut out: Vec<McpServer> =
            merged.iter().map(|(name, cfg)| McpServer::from_entry(name, cfg, tools_of(name), default_mode)).collect();
        // Still connected but no longer configured: live until the next reload.
        for (name, cfg) in &s.connections {
            if !merged.contains_key(name) {
                out.push(McpServer::from_entry(name, cfg, tools_of(name), default_mode));
            }
        }
        Ok(out)
    }

    /// Loaded MCP tools, optionally narrowed to one server.
    async fn mcp_tools(&self, ctx: &Context<'_>, server: Option<String>) -> Result<Vec<McpTool>> {
        let s = ctx.data::<EdgeData>()?.mcp.snapshot().await;
        Ok(match server {
            Some(name) => s.tools_for(&name).iter().map(|t| tool_type(&name, t)).collect(),
            None => s.connections.keys().flat_map(|name| s.tools_for(name).iter().map(move |t| tool_type(name, t))).collect(),
        })
    }
}

#[derive(Default)]
pub struct McpMutation;

#[Object]
impl McpMutation {
    async fn add_mcp_server(&self, ctx: &Context<'_>, name: String, config_json: String) -> Result<McpServer> {
        upsert_server(ctx, &name, &config_json, "config_json must be a JSON object (connection dict)").await
    }

    async fn update_mcp_server(&self, ctx: &Context<'_>, name: String, config_json: String) -> Result<McpServer> {
        upsert_server(ctx, &name, &config_json, "config_json must be a JSON object").await
    }

    async fn remove_mcp_server(&self, ctx: &Context<'_>, name: String) -> Result<bool> {
        let mcp = ctx.data::<EdgeData>()?.mcp.clone();
        let mut servers = config::from_db(mcp.pool()).await;
        if servers.shift_remove(&name).is_none() {
            let found = Value::Array(config::from_db(mcp.pool()).await.keys().map(|k| Value::String(k.clone())).collect());
            return Err(format!("MCP server {} not found in DB (found: {})", pyjson::repr_str(&name), pyjson::py_repr(&found)).into());
        }
        write_setting(mcp.pool(), config::SERVERS_KEY, &pyjson::dumps(&serde_json::to_value(&servers)?)).await?;
        crate::mcp::oauth::forget(mcp.pool(), &name).await?;
        mcp.reload().await;
        Ok(true)
    }

    async fn reload_mcp_servers(&self, ctx: &Context<'_>) -> Result<Vec<McpServer>> {
        let s = ctx.data::<EdgeData>()?.mcp.reload().await;
        Ok(s.connections.iter().map(|(name, cfg)| McpServer::from_snapshot(name, cfg, &s)).collect())
    }

    /// Bind this server's tools to the agent (`always`) or not (`lazy`).
    async fn set_mcp_server_load_mode(&self, ctx: &Context<'_>, name: String, mode: String) -> Result<McpServer> {
        let mcp = ctx.data::<EdgeData>()?.mcp.clone();
        let Some(mode) = config::normalize_mode(&Value::String(mode), Some(&name)) else {
            return Err(format!("mode must be one of {}", config::MODES.join(", ")).into());
        };
        if !mcp.snapshot().await.connections.contains_key(&name) {
            return Err(format!("MCP server {} is not configured", pyjson::repr_str(&name)).into());
        }
        let mut modes = config::load_modes(mcp.pool()).await;
        modes.insert(name.clone(), mode.into());
        write_setting(mcp.pool(), config::LOAD_MODES_KEY, &pyjson::dumps(&serde_json::to_value(&modes)?)).await?;
        let s = mcp.set_mode(&name, mode).await?;
        Ok(McpServer::from_snapshot(&name, &s.connections[&name], &s))
    }

    /// Fallback mode for servers that don't declare one.
    async fn set_mcp_default_load_mode(&self, ctx: &Context<'_>, mode: String) -> Result<String> {
        let mcp = ctx.data::<EdgeData>()?.mcp.clone();
        let Some(mode) = config::normalize_mode(&Value::String(mode), None) else {
            return Err(format!("mode must be one of {}", config::MODES.join(", ")).into());
        };
        write_setting(mcp.pool(), config::DEFAULT_MODE_KEY, mode).await?;
        mcp.reload().await;
        Ok(mode.into())
    }

    /// Invoke one MCP tool by (server, tool) — the lazy path's execution end.
    async fn call_mcp_tool(
        &self,
        ctx: &Context<'_>,
        server: String,
        tool: String,
        #[graphql(default_with = "\"{}\".to_string()")] args_json: String,
        #[graphql(default = 120.0)] timeout_seconds: f64,
    ) -> Result<McpToolResult> {
        let args = if args_json.is_empty() { json!({}) } else { parse_json(&args_json, "args_json")? };
        if !args.is_object() {
            return Err("args_json must be a JSON object".into());
        }
        let data = ctx.data::<EdgeData>()?;
        let pool = data.mcp.pool();
        let from = ctx.data::<RequestFrom>()?;
        if from.caller == Caller::Agent {
            let key = format!("mcp:{server}/{tool}");
            let (enabled, needs_approval) = policy(pool, &key).await?;
            if !enabled {
                return Err(format!("MCP tool {server}.{tool} is switched off in Settings \u{2192} Tools.").into());
            }
            if needs_approval {
                let label = format!("{server}.{tool}");
                if let Some(denial) = approve(ctx, &key, &label, &args, from.conversation.as_deref()).await? {
                    return Ok(McpToolResult { content: denial, is_error: true });
                }
            }
            let payload = json!({"server": server, "tool": tool, "args": args});
            super::approval::gate_action(ctx, "call_mcp_tool", payload).await?;
        }
        let result = data.mcp.call(&server, &tool, &args, Some(timeout_seconds)).await?;
        Ok(McpToolResult { content: result.text(), is_error: result.is_error })
    }
}

/// `await_tool_approval` for an agent's call: the request recorded and
/// shown on the conversation's live run, then waited on. `Some(denial)` when
/// a human said no or nobody answered.
async fn approve(ctx: &Context<'_>, key: &str, label: &str, args: &Value, conversation: Option<&str>) -> Result<Option<String>> {
    let pool = ctx.data::<EdgeData>()?.mcp.pool();
    let registry = ctx.data::<Arc<Registry>>()?;
    let run = super::approval::live_run(registry, conversation);
    let request = crate::approvals::create(pool, key, label, args, conversation, run.as_ref().map(|r| r.id.as_str())).await?;
    let live = run.as_ref().filter(|r| !r.fields().done);
    if let Some(r) = live {
        r.emit_local("approval_request", &request.event);
    }
    tracing::info!("tool gate: waiting on approval {} for {label}", request.id);
    let (approved, answer) = match crate::approvals::wait(pool, &request.id, crate::approvals::gate_timeout()).await? {
        crate::approvals::Outcome::Answered { approved, answer } => (approved, answer),
        crate::approvals::Outcome::TimedOut => {
            if let Some(r) = run.as_ref().filter(|r| !r.fields().done) {
                r.emit_local("approval_resolved", &crate::approvals::resolved_event(label, false, "timed out"));
            }
            (false, "timed out".to_string())
        }
    };
    tracing::info!("tool gate: {label} {} ({})", if approved { "approved" } else { "denied" }, request.id);
    Ok((!approved).then(|| crate::approvals::denial_message(label, &answer)))
}

/// `_exec_call_mcp_tool`: an approved deferred call, run.
pub async fn execute_approved(mcp: &Mcp, payload: &Value) -> Result<String> {
    let field = |key: &str| payload.get(key).map(pyjson::py_str).ok_or_else(|| format!("approval payload has no {key}"));
    let (server, tool) = (field("server")?, field("tool")?);
    let args = payload.get("args").filter(|a| pyjson::truthy(a)).cloned().unwrap_or_else(|| json!({}));
    let r = mcp.call(&server, &tool, &args, Some(crate::mcp::CALL_TIMEOUT)).await?;
    let prefix = if r.is_error { "MCP tool failed: " } else { "" };
    Ok(format!("{prefix}{}", r.text()).chars().take(4000).collect())
}
