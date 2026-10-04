//! Which chat turns and automation runs the edge's agent loop takes, decided
//! when they are queued. Anything it can't serve start to finish without Python's help
//! goes to Python as before; a turn that later needs something only Python
//! has is handed over mid-run (`queue::release`).

use std::path::Path;

use serde_json::Value;
use sqlx::SqlitePool;

use crate::catalog;

/// `JARVIS_AGENT_RUNTIME`: the edge runs the chat turns it can unless this
/// says `python`, which leaves every turn to Python.
pub fn enabled() -> bool {
    !std::env::var("JARVIS_AGENT_RUNTIME").is_ok_and(|v| v.trim().eq_ignore_ascii_case("python"))
}

/// Providers the edge's LLM layer speaks (`llm::call`), besides the
/// operator's own OpenAI-compatible endpoints.
const PROVIDERS: &[&str] = &["google_genai", "ollama", "openrouter", "meta"];

/// Whether the edge runs this turn: the agent loop is on, the model's
/// provider is one the edge calls, there are no attachments (reading them is
/// Python's), and no MCP server is configured (their tools live in Python's
/// MCP client).
pub async fn serves_chat(pool: &SqlitePool, model: &str, attachments: bool) -> bool {
    enabled() && !attachments && serves_model(pool, model).await
}

/// Whether the edge runs this automation: a code or webhook one, or a prompt
/// or monitor one on a model it serves.
pub async fn serves_automation(pool: &SqlitePool, automation_id: &str) -> bool {
    if !enabled() {
        return false;
    }
    let row: Option<(String, Option<String>)> =
        sqlx::query_as("SELECT input_type, model FROM automations WHERE id = ?").bind(automation_id).fetch_optional(pool).await.ok().flatten();
    let Some((input_type, model)) = row else { return false };
    if input_type == "code" || input_type == "webhook" {
        return true;
    }
    if input_type != "prompt" && input_type != "monitor" {
        return false;
    }
    match catalog::resolve_model(pool, model.as_deref()).await {
        Ok(model) => serves_model(pool, &model).await,
        Err(_) => false,
    }
}

/// The model's provider is one the edge calls, and no MCP server is
/// configured (their tools live in Python's MCP client).
pub async fn serves_model(pool: &SqlitePool, model: &str) -> bool {
    let Some((provider, _)) = model.split_once(':') else { return false };
    let known = PROVIDERS.contains(&provider)
        || catalog::endpoints(pool).await.is_ok_and(|eps| eps.iter().any(|e| e.name == provider));
    known && !mcp_configured(pool, &crate::config::app_dir()).await
}

/// Whether Python would find any MCP server configured — env, the first
/// config file, or the `mcp.servers` setting (`core/mcp.py`). Unsure means
/// yes: the turn then goes to Python, which is always right.
async fn mcp_configured(pool: &SqlitePool, app_dir: &Path) -> bool {
    let env = std::env::var("JARVIS_MCP_SERVERS").ok().filter(|v| !v.is_empty()).or_else(|| std::env::var("MCP_SERVERS").ok());
    if env.is_some_and(|raw| configures(&raw)) {
        return true;
    }
    // `_load_from_files`: ~/.jarvis/mcp.json, then mcp.json in Python's
    // working directory, which is the checkout.
    let home = std::env::var_os("HOME").map(|h| Path::new(&h).join(".jarvis").join("mcp.json"));
    for path in home.into_iter().chain([app_dir.join("mcp.json")]) {
        if let Ok(raw) = std::fs::read_to_string(&path) {
            if configures(&raw) {
                return true;
            }
        }
    }
    match catalog::setting(pool, "mcp.servers").await {
        Ok(raw) => raw.is_some_and(|raw| configures(&raw)),
        Err(_) => true,
    }
}

/// Whether `_normalize_servers` would find at least one server in `raw`.
fn configures(raw: &str) -> bool {
    let Ok(mut value) = serde_json::from_str::<Value>(raw) else { return false };
    if let Value::Object(map) = &value {
        for key in ["mcpServers", "servers", "mcp_servers"] {
            if let Some(inner @ Value::Object(_)) = map.get(key) {
                value = inner.clone();
                break;
            }
        }
    }
    match value {
        Value::Object(map) => map.values().any(Value::is_object),
        Value::Array(items) => items.iter().any(|item| {
            ["name", "id"].iter().any(|k| item.get(k).is_some_and(|v| !v.is_null() && v != "" && v != false && v != 0))
        }),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::configures;

    #[test]
    fn server_shapes_match_normalize_servers() {
        for (raw, want) in [
            (r#"{"mcpServers": {"fs": {"command": "x"}}}"#, true),
            (r#"{"servers": {"fs": {"url": "http://x"}}}"#, true),
            (r#"{"fs": {"command": "x"}}"#, true),
            (r#"[{"name": "fs", "command": "x"}]"#, true),
            (r#"[{"id": "fs"}]"#, true),
            (r#"{"mcpServers": {}}"#, false),
            (r#"{"fs": "not a config"}"#, false),
            (r#"[{"command": "x"}]"#, false),
            (r#"[]"#, false),
            (r#"{}"#, false),
            ("not json", false),
        ] {
            assert_eq!(configures(raw), want, "{raw}");
        }
    }
}
