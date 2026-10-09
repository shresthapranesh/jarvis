//! Outbound notifications when a run finishes — a port of
//! `core/notifications.py:send_notifications`. A run's `notifications`
//! column lists channel refs (`[{id, on}]`); each matching channel gets one
//! message straight through the Telegram Bot API or Discord's REST API, as
//! Python sends them. Best-effort: a failed send is logged, never raised.

use std::time::Duration;

use serde_json::{Value, json};
use sqlx::SqlitePool;

/// Room for the `[STATUS] title` header within Telegram's 4096 limit.
const MAX_TELEGRAM: usize = 3800;
/// Discord's hard limit is 2000; leave headroom.
const MAX_DISCORD: usize = 1900;

/// `parse_notifications`: the refs with a string `id`, `(id, on)`.
fn parse(raw: Option<&str>) -> Vec<(String, String)> {
    let Some(raw) = raw.filter(|r| !r.is_empty()) else { return vec![] };
    let Ok(Value::Array(items)) = serde_json::from_str::<Value>(raw) else {
        tracing::warn!("notifications config is not a JSON list; ignoring: {raw:?}");
        return vec![];
    };
    items
        .iter()
        .filter_map(|c| {
            let id = c.get("id")?.as_str()?;
            // `ref.get("on", "both")`: absent is both; a null or odd value matches nothing.
            let on = match c.get("on") {
                None => "both",
                Some(v) => v.as_str().unwrap_or(""),
            };
            Some((id.to_string(), on.to_string()))
        })
        .collect()
}

fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `_build_text`.
fn text(status: &str, title: &str, body: &str) -> String {
    let header = if status == "done" { title.to_string() } else { format!("[{}] {title}", status.to_uppercase()) };
    let body = if body.chars().count() <= MAX_TELEGRAM { body.to_string() } else { format!("{}…", head(body, MAX_TELEGRAM)) };
    format!("{header}\n\n{body}")
}

/// `send_notifications(session, raw, status=…, title=…, body=…)`.
pub async fn send(pool: &SqlitePool, raw: Option<&str>, status: &str, title: &str, body: &str) {
    let refs = parse(raw);
    if refs.is_empty() {
        return;
    }
    let message = text(status, title, body);
    for (id, on) in refs {
        let channel: Option<(String, String)> =
            match sqlx::query_as("SELECT type, target FROM notification_channels WHERE id = ?").bind(&id).fetch_optional(pool).await {
                Ok(c) => c,
                Err(e) => {
                    tracing::warn!("notification channel {id}: {e}");
                    continue;
                }
            };
        let Some((kind, target)) = channel else {
            tracing::warn!("notification refs missing channel {id}; skipping");
            continue;
        };
        if on != "both" && on != status {
            continue;
        }
        let sent = match kind.as_str() {
            "telegram" => telegram(&target, &message).await,
            "discord" => discord(&target, &message).await,
            other => {
                tracing::warn!("unknown channel type {other:?}; skipping");
                Ok(())
            }
        };
        if let Err(e) = sent {
            tracing::warn!("notification dispatch failed ({kind}): {e}");
        }
    }
}

fn var(k: &str) -> Option<String> {
    std::env::var(k).ok().filter(|v| !v.is_empty())
}

async fn telegram(chat_id: &str, text: &str) -> Result<(), String> {
    let Some(token) = var("TELEGRAM_BOT_TOKEN") else {
        tracing::warn!("telegram bot not configured; skipping notification to {chat_id}");
        return Ok(());
    };
    let base = var("TELEGRAM_API_URL").unwrap_or_else(|| "https://api.telegram.org".into());
    let mut client = reqwest::Client::builder().timeout(Duration::from_secs(30));
    if let Some(proxy) = var("TELEGRAM_PROXY_URL") {
        client = client.proxy(reqwest::Proxy::all(proxy).map_err(|e| e.to_string())?);
    }
    let client = client.build().map_err(|e| e.to_string())?;
    let resp = client
        .post(format!("{}/bot{token}/sendMessage", base.trim_end_matches('/')))
        .json(&json!({"chat_id": chat_id, "text": text}))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let body: Value = resp.json().await.map_err(|e| e.to_string())?;
    if body["ok"] != json!(true) {
        tracing::warn!("telegram sendMessage to {chat_id} failed: {}", body["description"]);
    }
    Ok(())
}

async fn discord(channel_id: &str, text: &str) -> Result<(), String> {
    let Some(token) = var("DISCORD_BOT_TOKEN") else {
        tracing::warn!("discord bot not configured; skipping notification to {channel_id}");
        return Ok(());
    };
    if channel_id.is_empty() || !channel_id.chars().all(|c| c.is_ascii_digit()) {
        tracing::warn!("invalid discord channel_id: {channel_id}");
        return Ok(());
    }
    let base = var("DISCORD_API_URL").unwrap_or_else(|| "https://discord.com/api/v10".into());
    let out = if text.chars().count() <= MAX_DISCORD { text.to_string() } else { format!("{}…", head(text, MAX_DISCORD - 1)) };
    let resp = reqwest::Client::new()
        .post(format!("{}/channels/{channel_id}/messages", base.trim_end_matches('/')))
        .header("Authorization", format!("Bot {token}"))
        .json(&json!({"content": out, "allowed_mentions": {"parse": []}}))
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if resp.status().as_u16() >= 400 {
        let status = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        tracing::warn!("discord message to {channel_id} failed: {status} {}", head(&body, 200));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refs_and_text_match_python() {
        assert_eq!(
            parse(Some(r#"[{"id": "a", "on": "error"}, {"id": "b"}, {"name": "legacy"}, {"id": 3}]"#)),
            vec![("a".into(), "error".into()), ("b".into(), "both".into())]
        );
        assert!(parse(Some("{}")).is_empty() && parse(None).is_empty() && parse(Some("nope")).is_empty());
        assert_eq!(text("done", "Daily", "ok"), "Daily\n\nok");
        assert_eq!(text("error", "Daily", "boom"), "[ERROR] Daily\n\nboom");
        let long = "é".repeat(MAX_TELEGRAM + 1);
        assert_eq!(text("done", "T", &long), format!("T\n\n{}…", "é".repeat(MAX_TELEGRAM)));
    }
}
