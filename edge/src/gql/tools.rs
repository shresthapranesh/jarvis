//! The tool inventory and its policy — `core/tool_policy.py`
//! (`tool_inventory`, `set_tool_policy`), `types/tool.py`, `queries/tool.py`
//! and `setToolPolicy` in `mutations/tool.py`. Change both.
//!
//! Bound tools are listed here as Python lists them; the `jarvis` SDK's are
//! Python's own catalogue (`sdk_tools.json`, exported and diffed by the
//! tests); MCP tools are what the edge's MCP manager has loaded.

use std::sync::LazyLock;

use async_graphql::{Context, ID, Object, Result, SimpleObject};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::SqlitePool;

use super::EdgeData;
use super::settings::upsert;
use crate::pyjson;

const CONFIG_KEY: &str = "tools.policy";

/// `_BOUND_TOOLS`, in order.
const BOUND: &[(&str, &str)] = &[
    ("run_cell", "Run Python in the conversation's stateful kernel — the door to the jarvis SDK."),
    ("write_artifact", "Save a versioned deliverable (markdown or an on-disk file) to the side panel."),
    ("write_todos", "Replace the run's task list (a graph state delta)."),
    ("set_todo_status", "Advance one todo item (a graph state delta)."),
    ("spawn_workers", "Run parallel role-templated subagents on this conversation's model."),
    ("run_workflow", "Invoke a saved workflow graph as a sub-agent."),
    ("remember", "Write a long-term memory item. Bound only when an embedder is configured."),
    ("complete_task", "Finish the current board task. Bound only inside a board run."),
    ("block_task", "Block the current board task or ask its owner a question. Board runs only."),
];

/// `_BOUND_CONDITIONAL`. `remember`'s note only shows without an embedder,
/// and there always is one (Gemini with a key, else Ollama).
const BOUND_DETAIL: &[(&str, &str)] = &[("complete_task", "board runs only"), ("block_task", "board runs only")];

#[derive(Deserialize)]
struct SdkTool {
    name: String,
    description: String,
    group: String,
}

static SDK: LazyLock<Vec<SdkTool>> =
    LazyLock::new(|| serde_json::from_str(include_str!("sdk_tools.json")).expect("sdk_tools.json is the SDK catalogue"));

/// One row of the inventory; `id` is the policy key.
#[derive(SimpleObject)]
pub struct AgentTool {
    id: ID,
    key: String,
    kind: String,
    name: String,
    description: String,
    group: String,
    enabled: bool,
    requires_approval: bool,
    in_prompt: bool,
    available: bool,
    detail: String,
}

/// `_parse`: the stored map, or empty for any reason at all.
fn parse(raw: Option<&str>) -> Map<String, Value> {
    match raw.filter(|r| !crate::pystr::strip(r).is_empty()).map(serde_json::from_str::<Value>) {
        Some(Ok(Value::Object(map))) => map,
        _ => Map::new(),
    }
}

/// `_coerce`: (enabled, approval).
fn coerce(entry: Option<&Value>) -> (bool, bool) {
    match entry {
        Some(Value::Object(e)) => (
            e.get("enabled").is_none_or(pyjson::truthy),
            e.get("approval").is_some_and(pyjson::truthy),
        ),
        _ => (true, false),
    }
}

/// `split_key`'s check: a known kind and a name after it.
fn known_key(key: &str) -> bool {
    key.split_once(':').is_some_and(|(kind, rest)| ["bound", "sdk", "mcp"].contains(&kind) && !rest.is_empty())
}

/// `tool_inventory`: bound, SDK, then each loaded MCP server's tools.
async fn inventory(pool: &SqlitePool, mcp: &crate::mcp::Mcp) -> Result<Vec<AgentTool>> {
    let policies = parse(crate::catalog::setting(pool, CONFIG_KEY).await?.as_deref());
    let row = |key: String, kind: &str, name: String, description: &str, group: &str, in_prompt: bool, detail: &str| {
        let (enabled, requires_approval) = coerce(policies.get(&key));
        AgentTool {
            id: ID(key.clone()),
            key,
            kind: kind.into(),
            name,
            description: description.into(),
            group: group.into(),
            enabled,
            requires_approval,
            in_prompt,
            available: true,
            detail: detail.into(),
        }
    };
    let bound = BOUND.iter().map(|(name, description)| {
        let detail = BOUND_DETAIL.iter().find(|(n, _)| n == name).map_or("", |(_, d)| *d);
        row(format!("bound:{name}"), "bound", name.to_string(), description, "agent", true, detail)
    });
    let sdk = SDK.iter().map(|t| row(format!("sdk:{}", t.name), "sdk", format!("jarvis.{}", t.name), &t.description, &t.group, false, ""));
    let mut out: Vec<AgentTool> = bound.chain(sdk).collect();
    // `_mcp_inventory`: a server that listed nothing has no rows; one that
    // listed tools is loaded, so every row is available.
    let s = mcp.snapshot().await;
    for server in s.connections.keys() {
        let in_prompt = s.mode(server) != crate::mcp::config::LAZY;
        for t in s.tools_for(server) {
            let description = crate::pystr::strip(&t.description);
            let first = crate::pystr::splitlines(description).first().copied().unwrap_or("");
            out.push(row(format!("mcp:{server}/{}", t.name), "mcp", t.name.clone(), first, server, in_prompt, ""));
        }
    }
    Ok(out)
}

#[derive(Default)]
pub struct ToolQuery;

#[Object]
impl ToolQuery {
    /// Bound tools, `jarvis` SDK functions and MCP tools, with their policy.
    async fn tools(&self, ctx: &Context<'_>) -> Result<Vec<AgentTool>> {
        inventory(ctx.data::<SqlitePool>()?, &ctx.data::<EdgeData>()?.mcp).await
    }
}

#[derive(Default)]
pub struct ToolPolicyMutation;

#[Object]
impl ToolPolicyMutation {
    /// Set one tool's policy; returns the whole refreshed inventory. Only
    /// non-default entries are stored.
    async fn set_tool_policy(
        &self,
        ctx: &Context<'_>,
        key: String,
        enabled: Option<bool>,
        requires_approval: Option<bool>,
    ) -> Result<Vec<AgentTool>> {
        if !known_key(&key) {
            return Err(format!("unknown tool key {}", pyjson::repr_str(&key)).into());
        }
        let pool = ctx.data::<SqlitePool>()?;
        let mut tx = crate::db::write_tx(pool).await?;
        let raw: Option<String> = sqlx::query_scalar("SELECT value FROM config_settings WHERE key = ?")
            .bind(CONFIG_KEY)
            .fetch_optional(&mut *tx)
            .await?;
        let mut stored = parse(raw.as_deref());
        let (cur_enabled, cur_approval) = coerce(stored.get(&key));
        let enabled = enabled.unwrap_or(cur_enabled);
        let approval = requires_approval.unwrap_or(cur_approval);
        if enabled && !approval {
            stored.shift_remove(&key);
        } else {
            stored.insert(key, json!({"enabled": enabled, "approval": approval}));
        }
        upsert(&mut tx, CONFIG_KEY, &pyjson::dumps(&Value::Object(stored))).await?;
        tx.commit().await?;
        inventory(pool, &ctx.data::<EdgeData>()?.mcp).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_and_entries_read_as_python_reads_them() {
        assert!(known_key("bound:run_cell") && known_key("mcp:gh/x") && known_key("sdk:a:b"));
        assert!(!known_key("bound:") && !known_key("tool:x") && !known_key("run_cell"));
        assert_eq!(coerce(Some(&json!({"enabled": 0, "approval": "yes"}))), (false, true));
        assert_eq!(coerce(Some(&json!("junk"))), (true, false));
        assert_eq!(parse(Some("  ")).len(), 0);
        assert_eq!(parse(Some("[1]")).len(), 0);
    }
}
