//! `browserAvailable` — whether a CDP browser answers at the configured
//! endpoint (`queries/browser.py`). The chat page asks on every load, so it is
//! served here rather than by starting Python to make one local request.

use std::time::Duration;

use async_graphql::{Context, Object, Result};
use sqlx::SqlitePool;

use super::EdgeData;

/// `tools/browser.py:DEFAULT_CDP_URL`.
const DEFAULT_CDP_URL: &str = "http://127.0.0.1:9222";
/// `_PROBE_TIMEOUT`.
const PROBE_TIMEOUT: Duration = Duration::from_secs(1);

#[derive(Default)]
pub struct BrowserQuery;

#[Object]
impl BrowserQuery {
    /// Never fails, and never launches a browser: a page load must not open
    /// a window.
    async fn browser_available(&self, ctx: &Context<'_>) -> Result<bool> {
        let pool = ctx.data::<SqlitePool>()?;
        match reachable(pool, &ctx.data::<EdgeData>()?.http).await {
            Some(up) => Ok(up),
            None => Err(super::defer(format!("browserAvailable: {} isn't plain http", cdp_url(pool).await))),
        }
    }
}

/// `_endpoint_live(cdp_url())`, or `None` when the endpoint isn't plain
/// http — the edge speaks no TLS; Python's probe does.
pub async fn reachable(pool: &SqlitePool, http: &reqwest::Client) -> Option<bool> {
    let url = cdp_url(pool).await;
    if !url.get(..7).is_some_and(|s| s.eq_ignore_ascii_case("http://")) {
        return None;
    }
    let probe = http.get(format!("{}/json/version", url.trim_end_matches('/'))).timeout(PROBE_TIMEOUT).send().await;
    Some(probe.is_ok_and(|r| r.status() == reqwest::StatusCode::OK))
}

/// `cdp_url()`: the `browser.cdp_url` setting, else `JARVIS_BROWSER_CDP_URL`,
/// else the default port on loopback.
async fn cdp_url(pool: &SqlitePool) -> String {
    let stored: Option<String> = sqlx::query_scalar("SELECT value FROM config_settings WHERE key = 'browser.cdp_url'")
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
    // Written as plain text, but a JSON-quoted value is tolerated.
    let setting = stored.map(|v| match serde_json::from_str::<serde_json::Value>(&v) {
        Ok(serde_json::Value::String(s)) => s,
        _ => v,
    });
    [setting, std::env::var("JARVIS_BROWSER_CDP_URL").ok()]
        .into_iter()
        .flatten()
        .map(|v| v.trim().to_string())
        .find(|v| !v.is_empty())
        .unwrap_or_else(|| DEFAULT_CDP_URL.to_string())
}

/// `browserActivity` (`mutations/browser.py` — change both): the kernel's
/// "I am in the browser right now", as a `browser_step` on the
/// conversation's live run. A worker's run is Python's to append to.
#[derive(Default)]
pub struct BrowserMutation;

#[Object]
impl BrowserMutation {
    /// Whether it reached a run; false (no live run) is normal, not an error.
    async fn browser_activity(
        &self,
        ctx: &Context<'_>,
        url: String,
        #[graphql(default_with = "\"start\".to_string()")] phase: String,
        conversation_id: Option<String>,
    ) -> Result<bool> {
        let from = ctx.data::<super::RequestFrom>()?;
        if from.caller != super::router::Caller::Agent {
            return Err("browserActivity is only for agent-initiated calls".into());
        }
        if !["start", "done", "error"].contains(&phase.as_str()) {
            return Err("phase must be one of: start, done, error".into());
        }
        let conversation = conversation_id.filter(|c| !c.is_empty()).or_else(|| from.conversation.clone());
        let Some(run) = super::approval::live_run(ctx.data::<std::sync::Arc<crate::runs::Registry>>()?, conversation.as_deref())
        else {
            return Ok(false);
        };
        if run.claimed() {
            return Err(super::defer("the browsing run is a worker's".into()));
        }
        let url = crate::pystr::prefix(&url, 500);
        run.emit_local("browser_step", &serde_json::json!({"url": url, "phase": phase, "source": "main"}));
        Ok(true)
    }
}
