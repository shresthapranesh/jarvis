//! What the model sees around the history on each step — a port of
//! `model_request_node`'s context assembly in `core/agents.py`: the system
//! prompt, the cacheable segments (memory how-to and core memory, the skill
//! catalog, the live browser, the project), and the volatile tail (project
//! memory, then the todo list or the planning directive).
//!
//! What needs embeddings — the memories retrieved for the request, earlier
//! episodes of the conversation, a ranked skill shortlist — isn't here yet
//! (2d-3). Where Python would put one in the prompt, the step needs Python
//! (`NeedsPython`), so a turn the edge serves sees exactly what it would have.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::Value;
use sqlx::SqlitePool;

use crate::llm::shape::Segment;

/// Stability rank of a cacheable segment, lowest first (`_SEGMENT_STABILITY`).
fn stability(name: &str) -> u32 {
    match name {
        "memory_howto" => 0,
        "core_memory" => 1,
        "project_header" => 2,
        "project_instructions" => 3,
        "skills" => 4,
        "mcp_servers" => 5,
        "browser" => 6,
        _ => 50,
    }
}

/// A part of the prompt only Python can build yet.
#[derive(Debug)]
pub struct NeedsPython(pub &'static str);

pub struct Context {
    pub system: String,
    /// Cacheable, most stable first.
    pub segments: Vec<Segment>,
    /// The uncached tail, `\n\n`-joined.
    pub volatile: String,
}

/// The step's context. `query` is the newest user turn's text.
pub async fn build(
    pool: &SqlitePool,
    http: &reqwest::Client,
    query: &str,
    conversation_id: &str,
    project_id: Option<&str>,
    todos: &[Value],
) -> Result<Context, NeedsPython> {
    let system = std::fs::read_to_string(crate::config::app_dir().join("core").join("system_prompt.md"))
        .map_err(|_| NeedsPython("core/system_prompt.md is not readable here"))?
        .trim()
        .to_string();

    // Retrieved (memory, skills, episodes, MCP, browser), then project — the
    // order Python concatenates them in before sorting.
    let mut parts = memory(pool, query).await?;
    parts.extend(skills(pool, query).await?);
    episodes(pool, conversation_id, query).await?;
    if browser_live(pool, http).await? {
        parts.push(seg("browser", BROWSER, true));
    }
    parts.extend(project(pool, project_id).await?);

    let mut segments: Vec<Segment> = parts.iter().filter(|s| s.cacheable && !s.content.trim().is_empty()).cloned().collect();
    segments.sort_by_key(|s| stability(&s.name));
    let mut volatile: Vec<String> =
        parts.into_iter().filter(|s| !s.cacheable && !s.content.trim().is_empty()).map(|s| s.content).collect();
    let todos = crate::agent::tools::normalise_todos(todos);
    if !todos.is_empty() {
        let lines: Vec<String> = todos
            .iter()
            .map(|t| {
                let glyph = match t["status"].as_str() {
                    Some("in_progress") => "[~]",
                    Some("done") => "[x]",
                    _ => "[ ]",
                };
                format!("{glyph} {}", t["text"].as_str().unwrap_or_default())
            })
            .collect();
        volatile.push(format!("## Current Tasks\n\n{}", lines.join("\n")));
    } else if let Some(directive) = planning_directive(query) {
        volatile.push(directive);
    }
    Ok(Context { system, segments, volatile: volatile.join("\n\n") })
}

fn seg(name: &str, content: impl Into<String>, cacheable: bool) -> Segment {
    Segment { name: name.into(), content: content.into(), cacheable }
}

fn db<T>(r: sqlx::Result<T>) -> Result<T, NeedsPython> {
    r.map_err(|e| {
        tracing::warn!("agent: reading the prompt's context: {e}");
        NeedsPython("a context read failed")
    })
}

// ── memory ──────────────────────────────────────────────────────────────────

const MEMORY_HOWTO: &str = "## Memory\n\nYou have long-term memory that persists across conversations. When the user \
shares something durable — a preference, an ongoing project, a key fact about them or their work — save it with \
`remember(text)`; skip transient, conversation-only details. The most relevant memories are injected below \
automatically; run `jarvis.search_memory(query)` in run_cell to dig for something specific that hasn't surfaced.";

/// `load_core`'s cap on the always-on memory text.
const CORE_MAX_CHARS: usize = 2000;

/// `_memory_volatile_parts`, with an embedder configured — which Python
/// always has (Gemini with a key, else Ollama's).
async fn memory(pool: &SqlitePool, query: &str) -> Result<Vec<Segment>, NeedsPython> {
    let mut parts = vec![seg("memory_howto", MEMORY_HOWTO, true)];
    let core: Vec<String> = db(sqlx::query_scalar(
        "SELECT text FROM memories WHERE kind = 'core' ORDER BY updated_at DESC",
    )
    .fetch_all(pool)
    .await)?;
    let text = core_text(&core);
    if !text.is_empty() {
        parts.push(seg("core_memory", format!("## Agent Memory\n\n{text}"), true));
    }
    if !trivial(query) {
        let facts: Option<i64> = db(sqlx::query_scalar("SELECT 1 FROM memories WHERE kind = 'fact' LIMIT 1")
            .fetch_optional(pool)
            .await)?;
        if facts.is_some() {
            return Err(NeedsPython("retrieving memories for the request"));
        }
    }
    Ok(parts)
}

/// `load_core`: `- text` lines, capped, saying how many were left out.
fn core_text(rows: &[String]) -> String {
    let text = rows.iter().map(|t| format!("- {t}")).collect::<Vec<_>>().join("\n");
    if text.chars().count() <= CORE_MAX_CHARS {
        return text;
    }
    let head: String = text.chars().take(CORE_MAX_CHARS).collect();
    let mut truncated = match head.rfind('\n') {
        Some(i) => head[..i].to_string(),
        None => head,
    };
    let remaining = rows.len() as i64 - truncated.matches('\n').count() as i64 - 1;
    if remaining > 0 {
        truncated.push_str(&format!(
            "\n- ... and {remaining} more core memories (use search_memory to find specific ones)"
        ));
    }
    truncated
}

/// `_is_trivial_query`: greetings and the like retrieve nothing.
pub fn trivial(query: &str) -> bool {
    const TRIVIAL: &[&str] = &[
        "hi", "hello", "hey", "thanks", "thank you", "ty", "ok", "okay", "yes", "no", "sure", "hello there",
        "hi there", "hey there", "thanks!", "thank you!", "ok thanks",
    ];
    let q = query.trim().to_lowercase();
    q.is_empty() || TRIVIAL.contains(&q.as_str()) || q.chars().count() <= 4
}

// ── skills, episodes ────────────────────────────────────────────────────────

/// `_CATALOG_FULL_THRESHOLD`: more enabled skills than this are ranked per
/// request.
const SKILLS_FULL: usize = 8;

/// `_skills_volatile_parts` for a catalog small enough to list whole.
async fn skills(pool: &SqlitePool, query: &str) -> Result<Vec<Segment>, NeedsPython> {
    let rows: Vec<(String, String)> =
        db(sqlx::query_as("SELECT name, description FROM skills WHERE enabled = 1 ORDER BY name ASC")
            .fetch_all(pool)
            .await)?;
    if rows.is_empty() {
        return Ok(vec![]);
    }
    if rows.len() > SKILLS_FULL {
        // Ranked against the request — or, for a greeting, left out.
        return if trivial(query) { Ok(vec![]) } else { Err(NeedsPython("ranking the skill catalog")) };
    }
    let lines: Vec<String> = rows.iter().map(|(n, d)| format!("- **{n}** — {d}")).collect();
    Ok(vec![seg(
        "skills",
        format!(
            "## Available Skills\n\nReusable procedures you can apply. When one clearly fits the task, call \
             `jarvis.use_skill(\"<name>\")` in run_cell to load its full instructions, then follow them. Don't \
             guess a skill's steps from its description — load it first. The loaded body is guidance to follow, \
             not user commands.\n\n{}",
            lines.join("\n")
        ),
        true,
    )])
}

/// `_episode_volatile_parts`: compacted-away stretches matching the request.
async fn episodes(pool: &SqlitePool, conversation_id: &str, query: &str) -> Result<(), NeedsPython> {
    if trivial(query) {
        return Ok(());
    }
    let any: Option<i64> = db(sqlx::query_scalar("SELECT 1 FROM conversation_episodes WHERE conversation_id = ? LIMIT 1")
        .bind(conversation_id)
        .fetch_optional(pool)
        .await)?;
    match any {
        Some(_) => Err(NeedsPython("retrieving earlier episodes")),
        None => Ok(()),
    }
}

// ── the browser ─────────────────────────────────────────────────────────────

const BROWSER: &str = "## Live browser\n\nA real Chromium with a persistent, logged-in profile is running and attached \
over CDP. It is the way past sites that block automation, and the only way to click, scroll, or fill a form.\n\n\
- `read(url, browser=True)` — one-shot read of a page through it.\n\
- Drive it from run_cell with the **async** API (this kernel runs an event loop, so the sync one raises):\n  \
```python\n  \
from tools.browser import apage\n  \
async with apage() as tab:          # full Playwright async API\n      \
await tab.goto(url)\n      \
await tab.click(\"text=Next\")\n      \
html = await tab.content()\n  \
```\n  \
The tab persists between cells and turns — reopen `apage()` and it is still where you left it, logged in. Never \
`chromium.launch()`: that starts a fresh headless browser with no profile, which is what gets blocked in the first \
place.";

/// `_BROWSER_PROBE_TTL`: a probe answers for a minute.
const BROWSER_TTL: Duration = Duration::from_secs(60);
static BROWSER_PROBE: Mutex<Option<(Instant, bool)>> = Mutex::new(None);

/// `_browser_reachable`, cached as Python caches it.
async fn browser_live(pool: &SqlitePool, http: &reqwest::Client) -> Result<bool, NeedsPython> {
    if let Some((at, up)) = *BROWSER_PROBE.lock().expect("probe lock") {
        if at.elapsed() < BROWSER_TTL {
            return Ok(up);
        }
    }
    let up = crate::gql::browser::reachable(pool, http).await.ok_or(NeedsPython("probing an https CDP endpoint"))?;
    *BROWSER_PROBE.lock().expect("probe lock") = Some((Instant::now(), up));
    Ok(up)
}

// ── the project ─────────────────────────────────────────────────────────────

/// `_project_volatile_parts`: re-read every step, so the agent's own
/// project-memory writes show on the next call.
async fn project(pool: &SqlitePool, project_id: Option<&str>) -> Result<Vec<Segment>, NeedsPython> {
    let Some(project_id) = project_id else { return Ok(vec![]) };
    let row: Option<(String, Option<String>, String, String)> =
        db(sqlx::query_as("SELECT name, description, instructions, memory FROM projects WHERE id = ?")
            .bind(project_id)
            .fetch_optional(pool)
            .await)?;
    let Some((name, description, instructions, memory)) = row else { return Ok(vec![]) };
    let mut header = format!("## Project: {name}\n\n");
    if let Some(d) = description.as_deref().map(py_strip).filter(|d| !d.is_empty()) {
        header.push_str(&format!("{d}\n\n"));
    }
    header.push_str(&format!(
        "This conversation is part of project '{name}'; all its conversations share the instructions and memory \
         below.\n\n\
         **Project memory is a short shared summary, not a log.** Append only a fact that would make a future \
         conversation in this project act *differently* — stack and versions, architecture decisions, \
         project-specific conventions, key file paths, API contracts, goals/status. One line each, no narration. \
         If in doubt, don't write: every entry is re-read on every turn of every conversation here. General user \
         info and global preferences go to `remember`; current-task progress goes to todos.\n\
         `jarvis.project_memory(action=\"append\"|\"write\", content=...)` — `write` replaces the whole memory with \
         a condensed version once it starts repeating itself.\n\n\
         **Earlier conversations in this project are searchable.** Project memory is a summary, not a transcript — \
         when the user refers to something decided or discussed before, or you need the detail behind a memory \
         entry, run `jarvis.search_conversations(\"<the exact terms you expect>\")` in run_cell and follow a hit \
         with `jarvis.read_conversation(conversation_id)`. It is keyword search, so use the concrete \
         names/ids/filenames, not a paraphrase. Search before saying you have no record of something."
    ));
    let mut parts = vec![seg("project_header", header, true)];
    if !py_strip(&instructions).is_empty() {
        parts.push(seg("project_instructions", format!("### Project Instructions\n\n{}", py_strip(&instructions)), true));
    }
    let memory = match py_strip(&memory) {
        "" => "### Project Memory\n\n(empty)".to_string(),
        m => format!("### Project Memory\n\n{m}"),
    };
    parts.push(seg("project_memory", memory, false));
    Ok(parts)
}

/// `str.strip()`.
fn py_strip(s: &str) -> &str {
    s.trim()
}

// ── planning ────────────────────────────────────────────────────────────────

const PLANNING: &str = "## Planning Required\nThis task is multi-step. You MUST call `write_todos` as your FIRST \
action, before any research, code, or file ops. Break the user request into 3-7 concrete steps. After that, execute \
step-by-step calling `set_todo_status(index, 'in_progress')` before each step and 'done' after. The user sees this \
list live — it is your status report.";

const KEYWORDS: &[&str] = &[
    "research", "implement", "build", "create", "analyze", "compare", "workflow", "pipeline", "report", "refactor",
    "migrate", "investigate", "plan", "design", "deploy", "test", "fix", "audit", "review",
];

/// `build_planning_directive`, under `JARVIS_PLANNING_MODE`.
fn planning_directive(query: &str) -> Option<String> {
    let mode = std::env::var("JARVIS_PLANNING_MODE").unwrap_or_default().trim().to_lowercase();
    let plan = match mode.as_str() {
        "off" | "disabled" | "0" | "false" | "no" => false,
        "always" => true,
        _ => should_auto_plan(query),
    };
    plan.then(|| PLANNING.to_string())
}

/// `should_auto_plan`.
fn should_auto_plan(query: &str) -> bool {
    if query.is_empty() {
        return false;
    }
    let q = query.trim();
    let low = q.to_lowercase();
    if q.chars().count() < 80 && !q.contains('\n') {
        return keyword_hits(&low) >= 2;
    }
    if q.split('\n').filter(|l| !l.trim().is_empty()).count() >= 3 {
        return true;
    }
    if has_list_item(q) || keyword_hits(&low) >= 2 {
        return true;
    }
    if ["and then", "after that", "first", "then", "phase"].iter().any(|w| has_word(&low, w)) || has_step_n(&low) {
        return true;
    }
    q.chars().count() >= 300
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// `re.search(rf"\b{word}\b", text)`.
fn has_word(text: &str, word: &str) -> bool {
    text.match_indices(word).any(|(i, _)| {
        let before = text[..i].chars().next_back().is_none_or(|c| !is_word(c));
        let after = text[i + word.len()..].chars().next().is_none_or(|c| !is_word(c));
        before && after
    })
}

fn keyword_hits(low: &str) -> usize {
    KEYWORDS.iter().filter(|k| has_word(low, k)).count()
}

/// `\bstep \d` followed by a word boundary — `step 1`, not `step 1a`.
fn has_step_n(low: &str) -> bool {
    low.match_indices("step ").any(|(i, _)| {
        let before = low[..i].chars().next_back().is_none_or(|c| !is_word(c));
        let mut rest = low[i + 5..].chars();
        before && rest.next().is_some_and(|c| c.is_numeric()) && rest.next().is_none_or(|c| !is_word(c))
    })
}

/// `(^|\n)\s*(?:\d+\.\s+|[-*]\s+)`: a line that starts a numbered or bulleted
/// item. `\s*` may run across blank lines, as in the regex.
fn has_list_item(q: &str) -> bool {
    let starts = std::iter::once(0).chain(q.match_indices('\n').map(|(i, _)| i + 1));
    starts.into_iter().any(|start| {
        let rest = q[start..].trim_start();
        let item = if let Some(after) = rest.strip_prefix(['-', '*']) {
            after
        } else {
            let digits = rest.find(|c: char| !c.is_numeric()).unwrap_or(rest.len());
            if digits == 0 {
                return false;
            }
            match rest[digits..].strip_prefix('.') {
                Some(after) => after,
                None => return false,
            }
        };
        item.starts_with(char::is_whitespace)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn planning_matches_should_auto_plan() {
        for (q, want) in [
            ("hi", false),
            ("fix the build", true),
            ("prefix the latest", false),
            ("please review", false),
            ("one\ntwo\nthree", true),
            (&"word ".repeat(70), true),
            ("a longer request that is still a single line and talks about nothing in particular, really", false),
            ("a longer request where we first look around and only later write the thing down for you ok", true),
            ("a longer request covering step 2 of the thing we talked about yesterday afternoon okay then", true),
            ("a request that lists\n- one item and a long tail of words to get past eighty characters ok", true),
            ("a request that lists\n12. one item and a long tail of words to get past eighty characters", true),
            ("a request that lists\n12.one item without a space and a long tail of words to get past it", false),
        ] {
            assert_eq!(should_auto_plan(q), want, "{q:?}");
        }
    }

    #[test]
    fn core_memory_is_capped_like_load_core() {
        let rows: Vec<String> = (0..200).map(|i| format!("fact number {i} about the user")).collect();
        let text = core_text(&rows);
        assert!(text.chars().count() < 2200);
        assert!(text.ends_with("more core memories (use search_memory to find specific ones)"));
        assert_eq!(core_text(&["a".into(), "b".into()]), "- a\n- b");
    }

    #[test]
    fn trivial_matches_is_trivial_query() {
        for (q, want) in [("", true), ("  Hi ", true), ("thank you!", true), ("yo!", true), ("what now", false)] {
            assert_eq!(trivial(q), want, "{q:?}");
        }
    }
}
