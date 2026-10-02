//! `/internal/worker` — the WebSocket the Python worker reports its runs over,
//! and the edge steers them through. The other end, and the protocol, are in
//! `core/edge_link.py`.
//!
//! Loopback only: whoever connects here can publish events into any run's
//! stream. The edge has no other authentication, and doesn't pretend this is
//! one — it's the same trust boundary as the rest of a loopback-bound server.

use std::net::SocketAddr;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{ConnectInfo, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::Value;
use tokio::sync::mpsc;

use crate::AppState;
use crate::runs::{Fields, Registry, Reported};
use crate::schedule::Scheduler;
use crate::supervisor::Supervisor;

pub const PROTOCOL: u64 = 4;

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
enum FromWorker {
    Hello {
        protocol: u64,
        instance: String,
    },
    Snapshot {
        tasks: Vec<Reported>,
    },
    Register(Reported),
    Events {
        task_id: String,
        from: usize,
        events: Vec<Value>,
    },
    State {
        task_id: String,
        #[serde(flatten)]
        fields: Fields,
    },
    Unregister {
        task_id: String,
    },
    /// Run a board dispatch pass now (a card became ready).
    Dispatch,
    /// An automation's schedule changed; re-read them.
    Schedules,
    /// Why the worker must not be stopped for being idle (`supervisor.rs`).
    Holds {
        holds: Vec<String>,
    },
    /// The answer to a `call` (`Registry::call`).
    Reply {
        id: u64,
        ok: bool,
        #[serde(default)]
        value: Value,
        #[serde(default)]
        error: Option<String>,
    },
}

pub async fn upgrade(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    ws: WebSocketUpgrade,
) -> Response {
    if !peer.ip().is_loopback() {
        return (StatusCode::FORBIDDEN, "worker link is loopback-only").into_response();
    }
    ws.max_message_size(usize::MAX)
        .max_frame_size(usize::MAX)
        .on_upgrade(move |socket| serve(socket, state.runs.clone(), state.scheduler.clone(), state.supervisor.clone()))
}

async fn serve(
    socket: WebSocket,
    registry: std::sync::Arc<Registry>,
    scheduler: std::sync::Arc<Scheduler>,
    supervisor: std::sync::Arc<Supervisor>,
) {
    let (mut tx, mut rx) = socket.split();

    // The first message must be a hello in a protocol this edge speaks.
    let instance = match rx.next().await {
        Some(Ok(Message::Text(text))) => match serde_json::from_str::<FromWorker>(&text) {
            Ok(FromWorker::Hello { protocol, instance }) if protocol == PROTOCOL => instance,
            Ok(FromWorker::Hello { protocol, .. }) => {
                tracing::error!("worker speaks link protocol {protocol}, edge speaks {PROTOCOL}; refusing");
                return;
            }
            _ => {
                tracing::warn!("worker link: expected hello");
                return;
            }
        },
        _ => return,
    };

    let (control_tx, mut control_rx) = mpsc::unbounded_channel::<String>();
    let session = registry.attach(instance.clone(), control_tx);
    tracing::info!("worker {instance} linked (session {session})");

    let writer = tokio::spawn(async move {
        while let Some(msg) = control_rx.recv().await {
            if tx.send(Message::text(msg)).await.is_err() {
                break;
            }
        }
    });

    while let Some(Ok(msg)) = rx.next().await {
        let Message::Text(text) = msg else { continue };
        match serde_json::from_str::<FromWorker>(&text) {
            Ok(FromWorker::Snapshot { tasks }) => registry.snapshot(session, tasks),
            Ok(FromWorker::Register(run)) => registry.register(session, run),
            Ok(FromWorker::Events { task_id, from, events }) => registry.events(session, &task_id, from, events),
            Ok(FromWorker::State { task_id, fields }) => registry.state(session, &task_id, fields),
            Ok(FromWorker::Unregister { task_id }) => registry.unregister(session, &task_id),
            Ok(FromWorker::Reply { id, ok, value, error }) => {
                let reply = if ok { Ok(value) } else { Err(error.unwrap_or_else(|| "the worker call failed".into())) };
                registry.reply(session, id, reply)
            }
            Ok(FromWorker::Dispatch) => {
                let scheduler = scheduler.clone();
                tokio::spawn(async move {
                    if let Err(e) = scheduler.dispatch().await {
                        tracing::error!("board dispatch failed: {e}");
                    }
                });
            }
            Ok(FromWorker::Schedules) => scheduler.schedules_changed(),
            Ok(FromWorker::Holds { holds }) => supervisor.set_holds(holds),
            Ok(FromWorker::Hello { .. }) => tracing::warn!("worker link: repeated hello ignored"),
            Err(e) => tracing::warn!("worker link: bad message: {e}"),
        }
    }

    registry.detach(session);
    writer.abort();
    tracing::info!("worker {instance} unlinked (session {session})");
}
