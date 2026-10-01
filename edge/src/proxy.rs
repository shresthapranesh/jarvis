//! Reverse proxy to the Python server for everything the edge doesn't serve
//! itself: un-ported GraphQL operations, subscriptions, REST, the live-audio
//! and browser WebSockets, and the SPA.
//!
//! Bodies are streamed both ways, never buffered: uploads are up to 100 MiB
//! and `/server-logs/stream` is a response that never ends.

use std::net::SocketAddr;

use axum::body::Body;
use axum::extract::ws::{self, WebSocket, WebSocketUpgrade};
use axum::extract::{ConnectInfo, FromRequestParts, Request, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::tungstenite::{self as tung, client::IntoClientRequest};

use crate::AppState;

/// Hop-by-hop headers (RFC 9110 §7.6.1) describe one connection, not the
/// message, so they are never forwarded.
fn is_hop_by_hop(name: &HeaderName) -> bool {
    matches!(
        name.as_str(),
        "connection"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "te"
            | "trailer"
            | "transfer-encoding"
            | "upgrade"
    )
}

/// Fallback handler: every request no edge route claimed.
pub async fn any(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    req: Request,
) -> Response {
    // `routes_logs.py:_require_localhost` checks the client address. Behind
    // the edge every request reaches Python from 127.0.0.1, so that check
    // would pass for anyone — the edge has to make it on the real peer.
    if req.uri().path().starts_with("/server-logs") && !peer.ip().is_loopback() {
        return (
            StatusCode::FORBIDDEN,
            [(header::CONTENT_TYPE, "application/json")],
            r#"{"error":"logs endpoint is localhost-only"}"#,
        )
            .into_response();
    }
    if is_websocket_upgrade(req.headers()) {
        return websocket(state, req).await;
    }
    http(&state, peer, req).await
}

pub async fn http(state: &AppState, peer: SocketAddr, req: Request) -> Response {
    let path = req.uri().path_and_query().map_or("/", |p| p.as_str()).to_string();
    let url = format!("{}{}", state.config.backend, path);
    let (parts, body) = req.into_parts();

    let mut headers = HeaderMap::with_capacity(parts.headers.len() + 2);
    for (name, value) in &parts.headers {
        if !is_hop_by_hop(name) {
            headers.append(name, value.clone());
        }
    }
    if let Ok(v) = HeaderValue::from_str(&peer.ip().to_string()) {
        headers.insert(HeaderName::from_static("x-forwarded-for"), v);
    }

    let upstream = state
        .http
        .request(parts.method, url)
        .headers(headers)
        .body(reqwest::Body::wrap_stream(body.into_data_stream()))
        .send()
        .await;

    match upstream {
        Ok(resp) => {
            let mut out = Response::builder().status(resp.status());
            if let Some(h) = out.headers_mut() {
                for (name, value) in resp.headers() {
                    if !is_hop_by_hop(name) {
                        h.append(name, value.clone());
                    }
                }
            }
            out.body(Body::from_stream(resp.bytes_stream()))
                .unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response())
        }
        Err(e) => {
            tracing::warn!("backend unreachable for {path}: {e}");
            (StatusCode::BAD_GATEWAY, "jarvis backend unavailable").into_response()
        }
    }
}

fn is_websocket_upgrade(headers: &HeaderMap) -> bool {
    headers
        .get(header::UPGRADE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("websocket"))
}

/// Headers worth carrying onto the backend handshake. The subprotocol is the
/// one that matters: graphql-ws and graphql-transport-ws are told apart by it.
const WS_FORWARDED: &[HeaderName] =
    &[header::SEC_WEBSOCKET_PROTOCOL, header::COOKIE, header::ORIGIN, header::USER_AGENT];

async fn websocket(state: AppState, req: Request) -> Response {
    let path = req.uri().path_and_query().map_or("/", |p| p.as_str()).to_string();
    let (mut parts, _) = req.into_parts();
    let upgrade = match WebSocketUpgrade::from_request_parts(&mut parts, &state).await {
        Ok(u) => u,
        Err(rejection) => return rejection.into_response(),
    };

    let mut backend_req = match format!("{}{}", state.config.backend_ws(), path).into_client_request() {
        Ok(r) => r,
        Err(e) => return (StatusCode::BAD_REQUEST, e.to_string()).into_response(),
    };
    for name in WS_FORWARDED {
        if let Some(v) = parts.headers.get(name) {
            backend_req.headers_mut().insert(name.clone(), v.clone());
        }
    }
    for (name, value) in &parts.headers {
        if name.as_str().starts_with("x-jarvis-") {
            backend_req.headers_mut().insert(name.clone(), value.clone());
        }
    }

    // Handshake with the backend first, so the client is offered exactly the
    // subprotocol the backend picked — or refused if the backend refused.
    let (backend, resp) = match tokio_tungstenite::connect_async(backend_req).await {
        Ok(pair) => pair,
        Err(e) => {
            tracing::warn!("backend websocket {path} failed: {e}");
            return (StatusCode::BAD_GATEWAY, "jarvis backend unavailable").into_response();
        }
    };
    let protocol = resp
        .headers()
        .get(header::SEC_WEBSOCKET_PROTOCOL)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let upgrade = match protocol {
        Some(p) => upgrade.protocols([p]),
        None => upgrade,
    };
    upgrade.on_upgrade(move |client| pump(client, backend))
}

type BackendSocket = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Shuttle frames both ways until either side closes. Pings and pongs stay
/// per-hop — each library answers its own peer's.
async fn pump(client: WebSocket, backend: BackendSocket) {
    let (mut client_tx, mut client_rx) = client.split();
    let (mut backend_tx, mut backend_rx) = backend.split();

    let upstream = async {
        while let Some(Ok(msg)) = client_rx.next().await {
            let out = match msg {
                ws::Message::Text(t) => tung::Message::text(t.as_str()),
                ws::Message::Binary(b) => tung::Message::binary(b),
                ws::Message::Close(frame) => {
                    let frame = frame.map(|f| tung::protocol::CloseFrame {
                        code: f.code.into(),
                        reason: f.reason.as_str().into(),
                    });
                    let _ = backend_tx.send(tung::Message::Close(frame)).await;
                    break;
                }
                ws::Message::Ping(_) | ws::Message::Pong(_) => continue,
            };
            if backend_tx.send(out).await.is_err() {
                break;
            }
        }
        let _ = backend_tx.close().await;
    };

    let downstream = async {
        while let Some(Ok(msg)) = backend_rx.next().await {
            let out = match msg {
                tung::Message::Text(t) => ws::Message::text(t.as_str()),
                tung::Message::Binary(b) => ws::Message::binary(b),
                tung::Message::Close(frame) => {
                    let frame = frame.map(|f| ws::CloseFrame { code: f.code.into(), reason: f.reason.as_str().into() });
                    let _ = client_tx.send(ws::Message::Close(frame)).await;
                    break;
                }
                tung::Message::Ping(_) | tung::Message::Pong(_) | tung::Message::Frame(_) => continue,
            };
            if client_tx.send(out).await.is_err() {
                break;
            }
        }
        let _ = client_tx.close().await;
    };

    // Whichever direction ends first tears the pair down.
    tokio::select! {
        () = upstream => {}
        () = downstream => {}
    }
}
