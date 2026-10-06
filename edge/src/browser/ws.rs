//! `/ws/browser`: live frames from the agent's browser — a port of
//! `server/routes_browser.py` (change both).
//!
//! JPEG frames are binary messages, not a GraphQL subscription: base64 would
//! inflate every frame by a third.
//!
//!   server → client  binary            one JPEG frame
//!   server → client  {"type":"meta"}   frame size + current page URL, on change
//!   server → client  {"type":"status"} "live" | "unavailable", with a reason
//!   server → client  {"type":"idle"}   no frame for a while; the socket lives
//!   client → server  anything          read (so a hang-up is seen), ignored

use std::time::Duration;

use axum::extract::State;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::response::Response;
use serde_json::{Value, json};

use crate::AppState;

/// `_IDLE_TIMEOUT`.
const IDLE_TIMEOUT: Duration = Duration::from_secs(30);

pub async fn upgrade(State(state): State<AppState>, ws: WebSocketUpgrade) -> Response {
    ws.on_upgrade(move |socket| serve(socket, state))
}

async fn send_json(socket: &mut WebSocket, value: Value) -> Result<(), axum::Error> {
    // Starlette's `send_json`: compact, non-ASCII as is.
    socket.send(Message::Text(value.to_string().into())).await
}

async fn serve(mut socket: WebSocket, state: AppState) {
    let sub = match state.screencast.subscribe().await {
        Ok(sub) => sub,
        Err(reason) => {
            // No browser, no display, nothing on the port: the panel shows why
            // rather than a spinner.
            tracing::info!("browser stream unavailable: {reason}");
            let _ = send_json(&mut socket, json!({"type": "status", "state": "unavailable", "reason": reason})).await;
            let _ = socket.send(Message::Close(None)).await;
            return;
        }
    };
    if send_json(&mut socket, json!({"type": "status", "state": "live"})).await.is_err() {
        return;
    }
    let mut frames = sub.frames.clone();
    let mut last_meta: Option<(i64, i64, String)> = None;
    loop {
        tokio::select! {
            incoming = socket.recv() => match incoming {
                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => return,
                Some(Ok(_)) => {}
            },
            changed = tokio::time::timeout(IDLE_TIMEOUT, frames.changed()) => match changed {
                // A still page paints nothing; prove the socket is alive.
                Err(_) => {
                    if send_json(&mut socket, json!({"type": "idle"})).await.is_err() {
                        return;
                    }
                }
                // The tab or the browser went away.
                Ok(Err(_)) => {
                    let reason = "the browser went away";
                    tracing::info!("browser stream unavailable: {reason}");
                    let _ = send_json(&mut socket, json!({"type": "status", "state": "unavailable", "reason": reason})).await;
                    let _ = socket.send(Message::Close(None)).await;
                    return;
                }
                Ok(Ok(())) => {
                    let Some(frame) = frames.borrow_and_update().clone() else { continue };
                    let meta = (frame.width, frame.height, frame.url.clone());
                    if last_meta.as_ref() != Some(&meta) {
                        let sent = send_json(
                            &mut socket,
                            json!({"type": "meta", "width": frame.width, "height": frame.height, "url": frame.url}),
                        )
                        .await;
                        if sent.is_err() {
                            return;
                        }
                        last_meta = Some(meta);
                    }
                    if socket.send(Message::Binary(frame.data.clone())).await.is_err() {
                        return;
                    }
                }
            },
        }
    }
}
