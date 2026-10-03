//! The v1 transcript record (`core/transcript_format.md`) — what a model sees
//! of a conversation, one message per `thread_messages` row. Python writes and
//! reads the same JSON (`core/transcript.py`), so a thread can move between
//! the two runtimes: a record read here and written back must be equal to the
//! one Python wrote.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const VERSION: u32 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
    Tool,
    System,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Message {
    #[serde(deserialize_with = "version")]
    pub v: u32,
    pub role: Role,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    pub content: Content,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_calls: Vec<ToolCall>,
    /// Calls the model emitted that didn't parse — kept, never sent back.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub invalid_tool_calls: Vec<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<Usage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<ModelRef>,
    /// As the provider spelled it; Python can write a `null` here, which
    /// stays a `null` rather than becoming absent.
    #[serde(default, deserialize_with = "present", skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    /// `success` or `error`, on a tool message.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub artifact: Option<Value>,
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub extras: Map<String, Value>,
}

fn version<'de, D: serde::Deserializer<'de>>(d: D) -> Result<u32, D::Error> {
    let v = u32::deserialize(d)?;
    if v != VERSION {
        return Err(serde::de::Error::custom(format!("transcript version {v} is not {VERSION}")));
    }
    Ok(v)
}

/// A field that is there, even as `null`.
fn present<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Option<Value>, D::Error> {
    Value::deserialize(d).map(Some)
}

impl Message {
    pub fn new(role: Role, content: Content) -> Self {
        Message {
            v: VERSION,
            role,
            id: None,
            name: None,
            content,
            tool_calls: vec![],
            invalid_tool_calls: vec![],
            usage: None,
            model: None,
            finish_reason: None,
            tool_call_id: None,
            status: None,
            artifact: None,
            extras: Map::new(),
        }
    }

    /// String content, or its text parts joined.
    #[cfg(test)]
    pub fn text(&self) -> String {
        match &self.content {
            Content::Text(s) => s.clone(),
            Content::Parts(parts) => parts
                .iter()
                .filter_map(|p| match p {
                    Part::Str(s) | Part::Typed(Typed::Text { text: s, .. }) => Some(s.as_str()),
                    _ => None,
                })
                .collect(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Content {
    Text(String),
    Parts(Vec<Part>),
}

/// A string — text with nothing attached — or a typed part.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Part {
    Str(String),
    Typed(Typed),
}

impl Part {
    /// The provider-specific part an `opaque` part keeps, and its `type`.
    pub fn opaque_type(&self) -> Option<&str> {
        match self {
            Part::Typed(Typed::Opaque { data, .. }) => data.get("type").and_then(Value::as_str),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Typed {
    Text {
        text: String,
        /// Gemini's thought signature on a text part.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        signature: Option<String>,
        #[serde(default, skip_serializing_if = "Map::is_empty")]
        extras: Map<String, Value>,
    },
    Thinking {
        thinking: String,
        /// Anthropic's signature; Gemini's on a thought part.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        signature: Option<String>,
        #[serde(default, skip_serializing_if = "Map::is_empty")]
        extras: Map<String, Value>,
    },
    RedactedThinking {
        data: String,
        #[serde(default, skip_serializing_if = "Map::is_empty")]
        extras: Map<String, Value>,
    },
    Image(Media),
    File(Media),
    /// A provider's own part, kept verbatim and in place.
    Opaque {
        data: Value,
        #[serde(default, skip_serializing_if = "Map::is_empty")]
        extras: Map<String, Value>,
    },
}

/// The bytes are in exactly one of `blob` (`sha256:<hex>`, a
/// `transcript_blobs` row), `data` (base64) or `url`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Media {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mime_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub blob: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub extras: Map<String, Value>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ToolCall {
    /// Always written; LangChain allows a call without one (`null`).
    pub id: Option<String>,
    pub name: String,
    pub args: Value,
    /// Gemini's thought signature for this call, sent back with it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Usage {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total: Option<i64>,
    /// `cache_read`, `cache_creation`, `audio`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_details: Option<Map<String, Value>>,
    /// `reasoning`, `audio`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_details: Option<Map<String, Value>>,
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub extras: Map<String, Value>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ModelRef {
    /// The catalog's provider id (`google_genai`, `ollama`, …).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn roundtrip(v: Value) {
        let msg: Message = serde_json::from_value(v.clone()).expect("parses");
        assert_eq!(serde_json::to_value(&msg).unwrap(), v);
    }

    #[test]
    fn every_shape_comes_back_equal() {
        roundtrip(json!({"v": 1, "role": "user", "content": "hi"}));
        roundtrip(json!({"v": 1, "role": "user", "id": "u1", "content": [
            "plain",
            {"type": "text", "text": "t", "extras": {"cache_control": {"type": "ephemeral"}}},
            {"type": "image", "mime_type": "image/png", "blob": "sha256:ab", "extras": {"lc_type": "image_url"}},
            {"type": "file", "mime_type": "application/pdf", "data": "AAA=", "extras": {"lc_type": "media"}},
            {"type": "image", "url": "https://x/y.png", "extras": {"lc_type": "image_url", "lc_image_url": "str"}},
        ]}));
        roundtrip(json!({"v": 1, "role": "assistant", "id": "a", "content": [
            {"type": "thinking", "thinking": "hm", "extras": {"index": 0}},
            {"type": "redacted_thinking", "data": "xx"},
            {"type": "text", "text": "ok", "signature": "c2ln"},
            {"type": "opaque", "data": {"type": "reasoning", "id": "rs_1", "summary": []}},
        ],
        "tool_calls": [{"id": "c1", "name": "run_cell", "args": {"code": "1"}, "signature": "s"},
                       {"id": null, "name": "x", "args": {}}],
        "invalid_tool_calls": [{"id": "c2", "name": "bad", "args": "{", "error": null}],
        "usage": {"input": 3, "output": 2, "total": 5, "input_details": {"cache_read": 0},
                  "output_details": {"reasoning": 1}, "extras": {"odd": 1}},
        "model": {"provider": "google_genai", "name": "gemma"},
        "finish_reason": null,
        "extras": {"additional_kwargs": {"function_call": {}}, "response_metadata": {"safety_ratings": []}}}));
        roundtrip(json!({"v": 1, "role": "tool", "name": "run_cell", "content": "r",
                         "tool_call_id": "c1", "status": "error", "artifact": {"k": [1]}}));
        roundtrip(json!({"v": 1, "role": "system", "content": "summary"}));
    }

    #[test]
    fn refuses_another_version() {
        assert!(serde_json::from_value::<Message>(json!({"v": 2, "role": "user", "content": ""})).is_err());
    }
}
