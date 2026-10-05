//! The WebSocket transport — `mcp.client.websocket.websocket_client`: the
//! `mcp` subprotocol, one JSON-RPC message per text frame.

use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

use super::config::Connection;
use super::session::Incoming;
use crate::pyjson;

type Stream = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

pub struct Ws {
    stream: Stream,
}

impl Ws {
    pub async fn open(params: &Connection) -> Result<Self, String> {
        let url = pyjson::py_str(&params["url"]);
        let mut req = url.as_str().into_client_request().map_err(|e| e.to_string())?;
        req.headers_mut().insert("sec-websocket-protocol", "mcp".parse().expect("a valid header value"));
        let (stream, _) = tokio_tungstenite::connect_async(req).await.map_err(|e| e.to_string())?;
        Ok(Ws { stream })
    }

    pub async fn send(&mut self, msg: &Value) -> Result<(), String> {
        let text = serde_json::to_string(msg).map_err(|e| e.to_string())?;
        self.stream.send(Message::Text(text.into())).await.map_err(|e| e.to_string())
    }

    pub async fn recv(&mut self) -> Incoming {
        while let Some(frame) = self.stream.next().await {
            match frame {
                Ok(Message::Text(text)) => match serde_json::from_str::<Value>(&text) {
                    Ok(msg) => return Incoming::Message(msg),
                    Err(_) => tracing::debug!("MCP websocket: a frame that isn't JSON"),
                },
                Ok(Message::Close(_)) => break,
                Ok(_) => {}
                Err(e) => return Incoming::Closed(e.to_string()),
            }
        }
        Incoming::Closed("Connection closed".into())
    }

    pub async fn close(mut self) {
        let _ = self.stream.close(None).await;
    }
}
