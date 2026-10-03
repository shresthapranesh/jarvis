//! The Jupyter messaging protocol (v5.3) — as much of it as running cells
//! needs. A message on the wire is a multipart ZeroMQ message:
//!
//! ```text
//! [identities…] <IDS|MSG> signature header parent_header metadata content [buffers…]
//! ```
//!
//! The signature is HMAC-SHA256, hex, over the four JSON parts, keyed with
//! the connection file's `key`.

use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::Sha256;
use zeromq::ZmqMessage;

const DELIMITER: &[u8] = b"<IDS|MSG>";
const VERSION: &str = "5.3";

/// A message from the kernel, signature checked.
#[derive(Debug)]
pub struct Msg {
    pub msg_type: String,
    /// The `msg_id` of the request it answers.
    pub parent_id: Option<String>,
    pub content: Value,
}

/// One client's identity toward one kernel: its key and session id.
#[derive(Clone)]
pub struct Signer {
    key: Vec<u8>,
    session: String,
}

impl Signer {
    pub fn new(key: &str) -> Self {
        Signer { key: key.as_bytes().to_vec(), session: uuid::Uuid::new_v4().to_string() }
    }

    fn mac(&self, parts: &[&[u8]]) -> Hmac<Sha256> {
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.key).expect("HMAC takes a key of any length");
        for p in parts {
            mac.update(p);
        }
        mac
    }

    /// A request, and its `msg_id` — what the kernel's replies and output
    /// name as their parent.
    pub fn request(&self, msg_type: &str, content: Value) -> (String, ZmqMessage) {
        let msg_id = uuid::Uuid::new_v4().simple().to_string();
        let header = json!({
            "msg_id": msg_id,
            "username": "jarvis",
            "session": self.session,
            "date": chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string(),
            "msg_type": msg_type,
            "version": VERSION,
        });
        let parts: [Vec<u8>; 4] = [header, json!({}), json!({}), content].map(|v| v.to_string().into_bytes());
        let signature = hex::encode(self.mac(&parts.each_ref().map(Vec::as_slice)).finalize().into_bytes());
        let mut frames: Vec<bytes::Bytes> = vec![DELIMITER.into(), signature.into()];
        frames.extend(parts.map(bytes::Bytes::from));
        (msg_id, ZmqMessage::try_from(frames).expect("frames"))
    }

    /// A kernel's message, or `None` for one that isn't well formed or
    /// isn't signed with our key.
    pub fn parse(&self, msg: ZmqMessage) -> Option<Msg> {
        let frames = msg.into_vec();
        let at = frames.iter().position(|f| f.as_ref() == DELIMITER)?;
        let [signature, header, parent, metadata, content] = frames.get(at + 1..at + 6)? else { return None };
        let mac = self.mac(&[header, parent, metadata, content]);
        if mac.verify_slice(&hex::decode(signature).ok()?).is_err() {
            tracing::warn!("kernel message with a bad signature — dropped");
            return None;
        }
        let header: Value = serde_json::from_slice(header).ok()?;
        let parent: Value = serde_json::from_slice(parent).ok()?;
        Some(Msg {
            msg_type: header.get("msg_type")?.as_str()?.to_string(),
            parent_id: parent.get("msg_id").and_then(Value::as_str).map(str::to_string),
            content: serde_json::from_slice(content).ok()?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_request_parses_back() {
        let s = Signer::new("secret");
        let (id, msg) = s.request("execute_request", json!({"code": "1"}));
        // Played back as if the kernel sent it, under one routing identity.
        let mut frames = msg.into_vec();
        frames.insert(0, bytes::Bytes::from_static(b"ident"));
        let parsed = s.parse(ZmqMessage::try_from(frames.clone()).unwrap()).unwrap();
        assert_eq!(parsed.msg_type, "execute_request");
        assert_eq!(parsed.content, json!({"code": "1"}));
        assert_eq!(parsed.parent_id, None);
        assert_eq!(id.len(), 32);
        // Someone else's key, or a changed byte, and it's dropped.
        assert!(Signer::new("other").parse(ZmqMessage::try_from(frames.clone()).unwrap()).is_none());
        frames[6] = bytes::Bytes::from_static(b"{\"code\": \"2\"}");
        assert!(s.parse(ZmqMessage::try_from(frames).unwrap()).is_none());
    }
}
