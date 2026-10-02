//! Reverse proxy to the Python server for everything the edge doesn't serve
//! itself: un-ported GraphQL operations, subscriptions, REST, the live-audio
//! and browser WebSockets, and the SPA.
//!
//! Bodies are streamed both ways, never buffered: uploads are up to 100 MiB
//! and `/server-logs/stream` is a response that never ends.
//!
//! When the edge owns the worker (`supervisor.rs`), a proxied request first
//! makes sure Python is up, and holds it up until the response — or the
//! socket — is finished. What a page load needs without Python is answered
//! here: the SPA's files, and `/health`.

use std::net::SocketAddr;
use std::path::Path;

use axum::body::Body;
use axum::extract::ws::{self, WebSocket, WebSocketUpgrade};
use axum::extract::{ConnectInfo, FromRequestParts, Request, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode, header};
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
    if matches!(*req.method(), Method::GET | Method::HEAD) {
        let path = req.uri().path();
        let head = *req.method() == Method::HEAD;
        if path == "/health" && state.supervisor.supervised() {
            // `routes_media.py:health`. A health check mustn't start Python.
            return ([(header::CONTENT_TYPE, "application/json")], r#"{"status":"ok"}"#).into_response();
        }
        if let Some(dir) = &state.config.static_dir {
            if !python_get_route(path) {
                return spa_file(dir, path, head).await;
            }
        }
    }
    http(&state, peer, req).await
}

/// The GET routes Python serves, which the SPA fallback must not shadow —
/// everything else a GET reaches is `entrypoint.py:spa_fallback`.
/// `tests/test_edge_supervisor.py` checks this against the Python app.
fn python_get_route(path: &str) -> bool {
    let raw = |prefix: &str| {
        path.strip_prefix(prefix)
            .and_then(|rest| rest.strip_suffix("/raw"))
            .is_some_and(|id| !id.is_empty() && !id.contains('/'))
    };
    matches!(
        path,
        "/health" | "/server-logs" | "/server-logs/stream" | "/graphql" | "/openapi.json" | "/docs"
            | "/docs/oauth2-redirect" | "/redoc"
    ) || raw("/artifacts/")
        || raw("/documents/")
}

/// `spa_fallback`: the file under the build if there is one, else
/// `index.html` for the client-side router.
async fn spa_file(dir: &Path, path: &str, head: bool) -> Response {
    let decoded = percent_encoding::percent_decode_str(path).decode_utf8_lossy();
    let rel = decoded.trim_start_matches('/');
    let contained = !rel.split('/').any(|seg| seg == "..") && !rel.contains(['\\', '\0']);
    let mut file = dir.join(if contained { rel } else { "" });
    if !tokio::fs::metadata(&file).await.is_ok_and(|m| m.is_file()) {
        file = dir.join("index.html");
    }
    let (Ok(meta), Ok(bytes)) = (tokio::fs::metadata(&file).await, tokio::fs::read(&file).await) else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    let mut out = Response::builder()
        .header(header::CONTENT_TYPE, content_type(&file))
        .header(header::CONTENT_LENGTH, bytes.len());
    if let Ok(modified) = meta.modified() {
        let at: chrono::DateTime<chrono::Utc> = modified.into();
        out = out.header(header::LAST_MODIFIED, at.format("%a, %d %b %Y %H:%M:%S GMT").to_string());
    }
    out.body(if head { Body::empty() } else { Body::from(bytes) })
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

/// What `mimetypes.guess_type` says for what a Vite build contains.
fn content_type(file: &Path) -> &'static str {
    let ext = file.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    match ext.as_str() {
        "html" | "htm" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" | "map" => "application/json",
        "webmanifest" => "application/manifest+json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "ico" => "image/vnd.microsoft.icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "txt" => "text/plain; charset=utf-8",
        "wasm" => "application/wasm",
        _ => "application/octet-stream",
    }
}

pub async fn http(state: &AppState, peer: SocketAddr, req: Request) -> Response {
    let activity = match state.supervisor.ensure_up().await {
        Ok(a) => a,
        Err(e) => return (StatusCode::SERVICE_UNAVAILABLE, e).into_response(),
    };
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
            // The worker stays up until the body is done: a log stream holds
            // it for as long as someone watches.
            let body = resp.bytes_stream().map(move |chunk| {
                let _held = &activity;
                chunk
            });
            out.body(Body::from_stream(body))
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

    let activity = match state.supervisor.ensure_up().await {
        Ok(a) => a,
        Err(e) => return (StatusCode::SERVICE_UNAVAILABLE, e).into_response(),
    };
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
    upgrade.on_upgrade(move |client| async move {
        pump(client, backend).await;
        drop(activity);
    })
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
