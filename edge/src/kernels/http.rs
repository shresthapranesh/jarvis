//! `/internal/kernels/*` — how Python behind the edge runs `run_cell` here
//! (`tools/code.py`), and shuts a session's kernel down.
//!
//! These run code, so beyond loopback-only they take nothing a browser can
//! send cross-site without asking first: the body must be
//! `application/json` (which a page can only send after a CORS preflight,
//! and none is answered), and a request with an `Origin` — every browser
//! POST from a page has one — is refused.
//!
//! A cell's caller going away (a cancelled run) drops the handler, which
//! interrupts the cell.

use std::net::SocketAddr;
use std::time::Duration;

use axum::Json;
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::json;

use super::{Cell, DEFAULT_CELL_TIMEOUT};
use crate::AppState;

#[derive(Deserialize)]
pub struct RunBody {
    key: String,
    code: String,
    #[serde(default)]
    timeout: Option<f64>,
    #[serde(default)]
    conversation_id: Option<String>,
    #[serde(default)]
    project_id: Option<String>,
}

#[derive(Deserialize)]
pub struct KeyBody {
    key: String,
}

fn refuse(peer: SocketAddr, headers: &HeaderMap) -> Option<Response> {
    let json = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.split(';').next().is_some_and(|t| t.trim().eq_ignore_ascii_case("application/json")));
    if !peer.ip().is_loopback() || headers.contains_key(header::ORIGIN) || !json {
        return Some((StatusCode::FORBIDDEN, "kernels are for the local worker").into_response());
    }
    None
}

pub async fn run(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    if let Some(r) = refuse(peer, &headers) {
        return r;
    }
    let body: RunBody = match serde_json::from_slice(&body) {
        Ok(b) => b,
        Err(e) => return (StatusCode::BAD_REQUEST, format!("body: {e}")).into_response(),
    };
    let timeout = body.timeout.filter(|t| t.is_finite() && *t > 0.0).unwrap_or(DEFAULT_CELL_TIMEOUT);
    let cell = Cell {
        code: &body.code,
        timeout: Duration::from_secs_f64(timeout),
        conversation_id: body.conversation_id.as_deref(),
        project_id: body.project_id.as_deref(),
    };
    match state.kernels.run(&body.key, &cell).await {
        Ok(output) => Json(json!({"output": output})).into_response(),
        Err(error) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": error}))).into_response(),
    }
}

pub async fn shutdown(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    with_key(peer, &headers, &body, |key| async move {
        state.kernels.shutdown(&key).await;
        json!({"ok": true})
    })
    .await
}

async fn with_key<F, Fut>(peer: SocketAddr, headers: &HeaderMap, body: &[u8], f: F) -> Response
where
    F: FnOnce(String) -> Fut,
    Fut: std::future::Future<Output = serde_json::Value>,
{
    if let Some(r) = refuse(peer, headers) {
        return r;
    }
    match serde_json::from_slice::<KeyBody>(body) {
        Ok(b) => Json(f(b.key).await).into_response(),
        Err(e) => (StatusCode::BAD_REQUEST, format!("body: {e}")).into_response(),
    }
}
