//! Which chat turns and automation runs the edge's agent loop takes, decided
//! when they are queued. Anything it can't serve start to finish without Python's help
//! goes to Python as before; a turn that later needs something only Python
//! has is handed over mid-run (`queue::release`).

use sqlx::SqlitePool;

use crate::catalog;

/// `JARVIS_AGENT_RUNTIME`: the edge runs the chat turns it can unless this
/// says `python`, which leaves every turn to Python.
pub fn enabled() -> bool {
    !std::env::var("JARVIS_AGENT_RUNTIME").is_ok_and(|v| v.trim().eq_ignore_ascii_case("python"))
}

/// Providers the edge's LLM layer speaks (`llm::call`), besides the
/// operator's own OpenAI-compatible endpoints.
const PROVIDERS: &[&str] = &["anthropic", "google_genai", "ollama", "openrouter", "meta"];

/// Whether the edge runs this turn: the agent loop is on and the model's
/// provider is one the edge calls.
pub async fn serves_chat(pool: &SqlitePool, model: &str) -> bool {
    enabled() && serves_model(pool, model).await
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

/// Whether the edge runs a board task's dispatch: its model is one it serves.
pub async fn serves_board(pool: &SqlitePool, model: Option<&str>) -> bool {
    if !enabled() {
        return false;
    }
    match catalog::resolve_model(pool, model).await {
        Ok(model) => serves_model(pool, &model).await,
        Err(_) => false,
    }
}

/// The model's provider is one the edge calls.
pub async fn serves_model(pool: &SqlitePool, model: &str) -> bool {
    calls_model(pool, model).await
}

/// The model's provider is one the edge's LLM layer calls — for Bedrock,
/// with credentials the edge can read (an AWS source only boto3 speaks is
/// Python's).
pub async fn calls_model(pool: &SqlitePool, model: &str) -> bool {
    let Some((provider, _)) = model.split_once(':') else { return false };
    if provider == "bedrock" {
        return !matches!(crate::aws::credentials().await, Err(crate::aws::CredError::Unsupported(_)));
    }
    PROVIDERS.contains(&provider) || catalog::endpoints(pool).await.is_ok_and(|eps| eps.iter().any(|e| e.name == provider))
}
