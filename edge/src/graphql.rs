//! `POST /graphql`: answer here when the edge owns the operation, else proxy.

use std::net::SocketAddr;

use axum::Json;
use axum::body::Body;
use axum::extract::{ConnectInfo, Request, State};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;

use crate::AppState;
use crate::gql::router::{Caller, Decision, decide};
use crate::proxy;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Body_ {
    query: String,
    operation_name: Option<String>,
    #[serde(default)]
    variables: Option<serde_json::Value>,
}

pub async fn post(State(state): State<AppState>, ConnectInfo(peer): ConnectInfo<SocketAddr>, req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let bytes = match axum::body::to_bytes(body, usize::MAX).await {
        Ok(b) => b,
        Err(e) => return (StatusCode::BAD_REQUEST, e.to_string()).into_response(),
    };

    let is_json = parts
        .headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/json"));
    // Batched arrays, multipart uploads and anything else unusual: Python.
    let parsed = is_json.then(|| serde_json::from_slice::<Body_>(&bytes).ok()).flatten();

    // Same reading as `server/graphql/context.py:get_context`.
    let caller = match parts.headers.get("x-jarvis-caller").and_then(|v| v.to_str().ok()) {
        Some(v) if v.trim().eq_ignore_ascii_case("agent") => Caller::Agent,
        _ => Caller::Human,
    };

    if let Some(op) = parsed {
        let variables = op.variables.unwrap_or(serde_json::Value::Null);
        match decide(&state.owned, &op.query, op.operation_name.as_deref(), &variables, caller) {
            Decision::Edge => {
                let mut request = async_graphql::Request::new(op.query)
                    .variables(async_graphql::Variables::from_json(variables));
                if let Some(name) = &op.operation_name {
                    request = request.operation_name(name);
                }
                let resp = state.schema.execute(request).await;
                if !failed_before_execution(&resp) {
                    return Json(resp).into_response();
                }
                // The root fields are ours but something under them isn't — a
                // field or argument this slice hasn't ported. Python can answer
                // it; this log is the to-do list.
                let why: Vec<_> = resp.errors.iter().map(|e| e.message.as_str()).collect();
                tracing::warn!("edge could not validate {:?}, proxying: {}", op.operation_name, why.join("; "));
            }
            Decision::Backend(why) => tracing::debug!("proxying {:?}: {why}", op.operation_name),
        }
    }

    proxy::http(&state, peer, Request::from_parts(parts, Body::from(bytes))).await
}

/// Parse or validation failed: nothing executed, so nothing has a `path`.
/// An execution error always carries the path of the field that raised it.
fn failed_before_execution(resp: &async_graphql::Response) -> bool {
    !resp.errors.is_empty()
        && resp.data == async_graphql::Value::Null
        && resp.errors.iter().all(|e| e.path.is_empty())
}
