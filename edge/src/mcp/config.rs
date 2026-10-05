//! Which MCP servers are configured — the config half of `core/mcp.py`.
//! Change both.
//!
//! Three sources, merged per server name, later winning: the
//! `JARVIS_MCP_SERVERS` env var, the first config file that names any server
//! (`~/.jarvis/mcp.json`, then `mcp.json` in the checkout), and the
//! `mcp.servers` setting. Each server's load mode rides in its connection
//! dict as `x-jarvis-load`, overlaid by the `mcp.load_modes` setting.

use std::path::Path;

use indexmap::IndexMap;
use serde_json::{Map, Value};
use sqlx::SqlitePool;

use crate::pyjson;

pub const LOAD_MODE_KEY: &str = "x-jarvis-load";
pub const ALWAYS: &str = "always";
pub const LAZY: &str = "lazy";
pub const MODES: [&str; 2] = [ALWAYS, LAZY];

pub const SERVERS_KEY: &str = "mcp.servers";
pub const LOAD_MODES_KEY: &str = "mcp.load_modes";
pub const DEFAULT_MODE_KEY: &str = "mcp.default_load_mode";

/// Jarvis-only keys, stripped before a config reaches a transport.
const JARVIS_KEYS: [&str; 2] = [LOAD_MODE_KEY, "x_jarvis_load"];

/// A server's connection dict, keys in the order they were written.
pub type Connection = Map<String, Value>;
pub type Servers = IndexMap<String, Connection>;

/// `normalize_load_mode`: a valid mode, or `None` for anything else (the
/// caller falls back to the default).
pub fn normalize_mode(value: &Value, server: Option<&str>) -> Option<&'static str> {
    if value.is_null() {
        return None;
    }
    let mode = crate::pystr::strip(&pyjson::py_str(value)).to_lowercase();
    if let Some(m) = MODES.iter().find(|m| **m == mode) {
        return Some(m);
    }
    tracing::warn!(
        "MCP server {}: unknown {LOAD_MODE_KEY}={} (expected one of {}) — using the default",
        server.unwrap_or("?"),
        pyjson::py_repr(value),
        MODES.join(", ")
    );
    None
}

/// The mode for servers that don't declare one: the `mcp.default_load_mode`
/// setting, else `JARVIS_MCP_DEFAULT_LOAD`, else `always`.
pub async fn default_mode(pool: &SqlitePool) -> &'static str {
    let setting = crate::catalog::setting(pool, DEFAULT_MODE_KEY).await.ok().flatten();
    let env = std::env::var("JARVIS_MCP_DEFAULT_LOAD").ok();
    setting
        .filter(|s| !s.is_empty())
        .and_then(|s| normalize_mode(&Value::String(s), None))
        .or_else(|| env.and_then(|e| normalize_mode(&Value::String(e), None)))
        .unwrap_or(ALWAYS)
}

/// `load_mode_for`: one server's mode from its connection dict.
pub fn mode_for(cfg: Option<&Connection>, default: &'static str) -> &'static str {
    let Some(cfg) = cfg else { return default };
    for key in JARVIS_KEYS {
        if let Some(v) = cfg.get(key) {
            return normalize_mode(v, None).unwrap_or(default);
        }
    }
    default
}

/// `strip_jarvis_keys`.
pub fn strip_jarvis_keys(cfg: &Connection) -> Connection {
    cfg.iter().filter(|(k, _)| !JARVIS_KEYS.contains(&k.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect()
}

/// `with_load_mode`: a copy carrying `mode`, at the end.
pub fn with_mode(cfg: &Connection, mode: &str) -> Connection {
    let mut out = strip_jarvis_keys(cfg);
    out.insert(LOAD_MODE_KEY.into(), Value::String(mode.into()));
    out
}

/// `_normalize_servers`: the shapes people write — Claude Desktop's
/// `{"mcpServers": {...}}`, `{name: config}`, `[{name, ...}]` — as
/// name → connection dict, with a `transport` guessed when absent.
pub fn normalize(raw: &Value) -> Servers {
    let mut raw = raw;
    if let Value::Object(map) = raw {
        for key in ["mcpServers", "servers", "mcp_servers"] {
            if let Some(inner @ Value::Object(_)) = map.get(key) {
                raw = inner;
                break;
            }
        }
    }
    let with_transport = |mut cfg: Connection| {
        if !cfg.contains_key("transport") {
            if cfg.contains_key("command") {
                cfg.insert("transport".into(), "stdio".into());
            } else if cfg.contains_key("url") {
                cfg.insert("transport".into(), "http".into());
            }
        }
        cfg
    };
    let mut out = Servers::new();
    match raw {
        Value::Object(map) => {
            for (name, cfg) in map {
                if let Value::Object(cfg) = cfg {
                    out.insert(name.clone(), with_transport(cfg.clone()));
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                let Value::Object(item) = item else { continue };
                let name = [item.get("name"), item.get("id")].into_iter().flatten().find(|v| pyjson::truthy(v));
                let Some(name) = name else { continue };
                let cfg = item.iter().filter(|(k, _)| *k != "name").map(|(k, v)| (k.clone(), v.clone())).collect();
                out.insert(pyjson::py_str(name), with_transport(cfg));
            }
        }
        _ => {}
    }
    out
}

fn parse(raw: &str) -> Option<Value> {
    serde_json::from_str(raw).ok()
}

fn from_env() -> Servers {
    let raw = std::env::var("JARVIS_MCP_SERVERS").ok().filter(|v| !v.is_empty()).or_else(|| std::env::var("MCP_SERVERS").ok());
    let Some(raw) = raw.filter(|v| !v.is_empty()) else { return Servers::new() };
    match parse(&raw) {
        Some(v) => normalize(&v),
        None => {
            tracing::warn!("Failed to parse JARVIS_MCP_SERVERS env JSON");
            Servers::new()
        }
    }
}

/// `_load_from_files`: the first candidate that names a server — they are
/// alternatives, never merged. Python's working directory is the checkout.
fn from_files(app_dir: &Path) -> Servers {
    let home = std::env::var_os("HOME").map(|h| Path::new(&h).join(".jarvis").join("mcp.json"));
    for path in home.into_iter().chain([app_dir.join("mcp.json")]) {
        let Ok(raw) = std::fs::read_to_string(&path) else { continue };
        let Some(value) = parse(&raw) else {
            tracing::warn!("Failed to read MCP config {}", path.display());
            continue;
        };
        let servers = normalize(&value);
        if !servers.is_empty() {
            return servers;
        }
    }
    Servers::new()
}

/// The `mcp.servers` setting, normalized.
pub async fn from_db(pool: &SqlitePool) -> Servers {
    match crate::catalog::setting(pool, SERVERS_KEY).await.ok().flatten().filter(|r| !r.is_empty()) {
        Some(raw) => parse(&raw).map(|v| normalize(&v)).unwrap_or_default(),
        None => Servers::new(),
    }
}

/// `get_mcp_load_modes_from_db`: the per-server overrides, as strings.
pub async fn load_modes(pool: &SqlitePool) -> IndexMap<String, String> {
    let raw = crate::catalog::setting(pool, LOAD_MODES_KEY).await.ok().flatten().filter(|r| !r.is_empty());
    match raw.as_deref().and_then(parse) {
        Some(Value::Object(map)) => map.into_iter().map(|(k, v)| (k, pyjson::py_str(&v))).collect(),
        _ => IndexMap::new(),
    }
}

/// `load_mcp_server_configs_with_db`: env < file < DB, then the mode
/// overrides. A name keeps the position it first had.
pub async fn merged(pool: &SqlitePool, app_dir: &Path) -> Servers {
    let mut out = from_env();
    out.extend(from_files(app_dir));
    out.extend(from_db(pool).await);
    for (name, mode) in load_modes(pool).await {
        let Some(cfg) = out.get_mut(&name) else { continue };
        if let Some(mode) = normalize_mode(&Value::String(mode), Some(&name)) {
            *cfg = with_mode(cfg, mode);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn names(v: Value) -> Vec<String> {
        normalize(&v).keys().cloned().collect()
    }

    #[test]
    fn shapes_match_normalize_servers() {
        assert_eq!(names(json!({"mcpServers": {"fs": {"command": "x"}}})), ["fs"]);
        assert_eq!(names(json!({"servers": {"fs": {"url": "http://x"}}})), ["fs"]);
        assert_eq!(names(json!({"fs": {"command": "x"}, "bad": "x"})), ["fs"]);
        assert_eq!(names(json!([{"name": "fs", "command": "x"}, {"id": 3}, {"name": ""}, {"command": "y"}])), ["fs", "3"]);
        assert!(names(json!({"mcpServers": {}})).is_empty());
        let s = normalize(&json!([{"name": "a", "id": "i", "url": "u"}]));
        assert_eq!(Value::Object(s["a"].clone()), json!({"id": "i", "url": "u", "transport": "http"}));
        let s = normalize(&json!({"a": {"command": "c", "transport": "sse"}}));
        assert_eq!(s["a"]["transport"], "sse");
    }

    #[test]
    fn modes() {
        let cfg = |v: Value| v.as_object().cloned().unwrap();
        assert_eq!(mode_for(Some(&cfg(json!({"x-jarvis-load": " Lazy "}))), ALWAYS), LAZY);
        assert_eq!(mode_for(Some(&cfg(json!({"x_jarvis_load": "lazy"}))), ALWAYS), LAZY);
        assert_eq!(mode_for(Some(&cfg(json!({"x-jarvis-load": "sometimes"}))), LAZY), LAZY);
        assert_eq!(mode_for(None, LAZY), LAZY);
        let moved = with_mode(&cfg(json!({"x-jarvis-load": "lazy", "command": "c"})), ALWAYS);
        assert_eq!(Value::Object(moved), json!({"command": "c", "x-jarvis-load": "always"}));
    }
}
