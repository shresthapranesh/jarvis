//! `/internal/mcp/*` — how Python behind the edge reaches MCP
//! (`core/mcp.py:EdgeMcp`): the loaded state, to bind and advertise the
//! same tools, and calls, run here. Loopback, JSON and no `Origin` only, as
//! the kernel endpoints: the state carries connection secrets, and a call
//! runs third-party code.
//!
//! A caller going away (a cancelled run) drops the call, which ends its
//! session and the server process with it.

use std::net::SocketAddr;

use axum::Json;
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::AppState;
use crate::kernels::http::refuse_for;

const WHY: &str = "MCP is for the local worker";

/// The manager's state as Python's `EdgeMcp` keeps it.
pub fn state_json(s: &super::Snapshot) -> Value {
    let servers: Vec<Value> = s
        .connections
        .iter()
        .map(|(name, cfg)| {
            let tools: Vec<Value> = s
                .tools_for(name)
                .iter()
                .map(|t| json!({"name": t.name, "description": t.description, "input_schema": t.input_schema}))
                .collect();
            json!({
                "name": name,
                "config": cfg,
                "load_mode": s.mode(name),
                "loaded": s.tools.contains_key(name),
                "tools": tools,
            })
        })
        .collect();
    json!({"default_load_mode": s.default_mode, "servers": servers})
}

pub async fn state(State(app): State<AppState>, ConnectInfo(peer): ConnectInfo<SocketAddr>, headers: HeaderMap) -> Response {
    if let Some(r) = refuse_for(peer, &headers, WHY) {
        return r;
    }
    Json(state_json(&*app.mcp.snapshot().await)).into_response()
}

#[derive(Deserialize)]
pub struct CallBody {
    server: String,
    tool: String,
    #[serde(default)]
    args: Value,
    /// Seconds; absent for a bound tool, which Python never times out.
    #[serde(default)]
    timeout: Option<f64>,
}

pub async fn call(
    State(app): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    if let Some(r) = refuse_for(peer, &headers, WHY) {
        return r;
    }
    let body: CallBody = match serde_json::from_slice(&body) {
        Ok(b) => b,
        Err(e) => return (StatusCode::BAD_REQUEST, format!("body: {e}")).into_response(),
    };
    let args = if body.args.is_null() { json!({}) } else { body.args };
    match app.mcp.call(&body.server, &body.tool, &args, body.timeout).await {
        Ok(r) => Json(json!({"blocks": r.blocks, "artifact": r.artifact, "is_error": r.is_error, "text": r.text()})).into_response(),
        Err(error) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": error}))).into_response(),
    }
}
