//! What no route claims: the REST routes `rest.rs` serves, `/health`, and the
//! SPA — the build's files, else `index.html` for the client-side router.

use std::net::SocketAddr;
use std::path::Path;

use axum::body::Body;
use axum::extract::{ConnectInfo, Request, State};
use axum::http::{Method, StatusCode, header};
use axum::response::{IntoResponse, Response};

use crate::AppState;

/// Fallback handler: every request no edge route claimed.
pub async fn any(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    req: Request,
) -> Response {
    // `routes_logs.py:_require_localhost`, on the real peer.
    if req.uri().path().starts_with("/server-logs") && !peer.ip().is_loopback() {
        return (
            StatusCode::FORBIDDEN,
            [(header::CONTENT_TYPE, "application/json")],
            r#"{"error":"logs endpoint is localhost-only"}"#,
        )
            .into_response();
    }
    let req = match crate::rest::serve(&state, req).await {
        Ok(response) => return response,
        Err(req) => req,
    };
    if matches!(*req.method(), Method::GET | Method::HEAD) {
        let path = req.uri().path();
        let head = *req.method() == Method::HEAD;
        if path == "/health" {
            return ([(header::CONTENT_TYPE, "application/json")], r#"{"status":"ok"}"#).into_response();
        }
        if let Some(dir) = &state.config.static_dir {
            return spa_file(dir, path, head).await;
        }
    }
    (StatusCode::NOT_FOUND, [(header::CONTENT_TYPE, "application/json")], r#"{"detail":"Not Found"}"#).into_response()
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
