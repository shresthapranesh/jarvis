//! `POST /graphql` and the subscription WebSocket on `GET /graphql`.

use async_graphql::http::ALL_WEBSOCKET_PROTOCOLS;
use async_graphql_axum::{GraphQLProtocol, GraphQLWebSocket};
use axum::Json;
use axum::extract::FromRequestParts;
use axum::extract::ws::WebSocketUpgrade;
use axum::extract::{Request, State};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::Value;

use crate::AppState;
use crate::gql::router::{Caller, unknown_operation};

/// One operation as the request carries it.
struct Operation {
    query: String,
    operation_name: Option<String>,
    variables: Value,
}

/// The body as Strawberry's `parse_http_body` reads it, refused as it
/// refuses one: a 400 with its message.
fn parse(content_type: Option<&str>, bytes: &[u8]) -> Result<Operation, &'static str> {
    // `parse_content_type`: the media type, case kept.
    let media = content_type.unwrap_or_default().split(';').next().unwrap_or_default().trim();
    if !media.contains("application/json") {
        // Multipart too: uploads aren't enabled.
        return Err("Unsupported content type");
    }
    let data = match serde_json::from_slice::<Value>(bytes) {
        Ok(Value::Object(data)) => data,
        Ok(Value::Array(_)) => return Err("Batching is not enabled"),
        Ok(_) | Err(_) => return Err("Unable to parse request body as JSON"),
    };
    let query = match data.get("query") {
        None | Some(Value::Null) => None,
        Some(Value::String(q)) => Some(q.clone()),
        Some(_) => return Err("The GraphQL operation's `query` must be a string or null, if provided."),
    };
    let variables = match data.get("variables") {
        None | Some(Value::Null) => Value::Null,
        Some(v @ Value::Object(_)) => v.clone(),
        Some(_) => return Err("The GraphQL operation's `variables` must be an object or null, if provided."),
    };
    if !matches!(data.get("extensions"), None | Some(Value::Null | Value::Object(_))) {
        return Err("The GraphQL operation's `extensions` must be an object or null, if provided.");
    }
    let query = query.ok_or("No GraphQL query found in the request")?;
    // Python takes a name that isn't a string as its `str()`.
    let operation_name = match data.get("operationName") {
        None | Some(Value::Null) => None,
        Some(Value::String(n)) => Some(n.clone()),
        Some(other) => Some(crate::pyjson::py_str(other)),
    };
    Ok(Operation { query, operation_name, variables })
}

pub async fn post(State(state): State<AppState>, req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let bytes = match axum::body::to_bytes(body, usize::MAX).await {
        Ok(b) => b,
        Err(e) => return (StatusCode::BAD_REQUEST, e.to_string()).into_response(),
    };

    let content_type = parts.headers.get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok());
    let op = match parse(content_type, &bytes) {
        Ok(op) => op,
        Err(why) => return (StatusCode::BAD_REQUEST, why).into_response(),
    };
    if let Some(why) = unknown_operation(&op.query, op.operation_name.as_deref()) {
        return (StatusCode::BAD_REQUEST, why).into_response();
    }

    // Same reading as `server/graphql/context.py:get_context` did.
    let caller = match parts.headers.get("x-jarvis-caller").and_then(|v| v.to_str().ok()) {
        Some(v) if v.trim().eq_ignore_ascii_case("agent") => Caller::Agent,
        _ => Caller::Human,
    };
    let conversation = parts
        .headers
        .get("x-jarvis-conversation")
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
        .map(str::to_string);
    let mut request = async_graphql::Request::new(op.query)
        .variables(async_graphql::Variables::from_json(op.variables))
        .data(crate::gql::RequestFrom { caller, conversation });
    if let Some(name) = &op.operation_name {
        request = request.operation_name(name);
    }
    Json(state.schema.execute(request).await).into_response()
}

/// `GET /graphql`: the subscription WebSocket.
pub async fn websocket(State(state): State<AppState>, req: Request) -> Response {
    let (mut parts, _) = req.into_parts();
    let protocol = match GraphQLProtocol::from_request_parts(&mut parts, &state).await {
        Ok(p) => p,
        Err(rejection) => return rejection.into_response(),
    };
    let upgrade = match WebSocketUpgrade::from_request_parts(&mut parts, &state).await {
        Ok(u) => u,
        Err(rejection) => return rejection.into_response(),
    };
    let schema = state.schema.clone();
    upgrade
        .protocols(ALL_WEBSOCKET_PROTOCOLS)
        .on_upgrade(move |socket| GraphQLWebSocket::new(socket, schema, protocol).serve())
}
