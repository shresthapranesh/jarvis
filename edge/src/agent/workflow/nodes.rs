//! `workflow/nodes.py`: the node types — a change is made in both.
//!
//! An `Err` is the exception Python's node raises, worded as `str(exc)`.
//! An approval or input node files its request in the `approvals` table and
//! waits on the row: a human answers it from the inbox, `resumeWorkflowRun`
//! or `resolveWorkflowApproval` (`gql/start.rs`, `gql/approval.rs`), which
//! close it with the answer.

use std::time::Duration;

use futures_util::{StreamExt, TryStreamExt};
use serde_json::{Map, Value, json};

use super::agent::run_text;
use super::engine::{self, Env, Halt, Output};
use super::template::{self, Scope};
use crate::{pyjson, pystr};

/// What a node runs in.
pub struct Ctx<'c, 'e> {
    pub env: &'c Env<'e>,
    pub node_id: &'c str,
    pub scope: Scope<'c>,
}

impl Ctx<'_, '_> {
    /// `_interpolate(template, inputs)`.
    fn render(&self, template: &str, inputs: &Map<String, Value>) -> String {
        template::render(template, inputs, self.scope)
    }

    fn token(&self, node_id: &str, text: &str) {
        self.env.emit("node_token", json!({"node_id": node_id, "text": text}));
    }

    async fn model(&self, config: &Map<String, Value>) -> Result<String, String> {
        resolve(self.env, config.get("model")).await
    }

    async fn ask(&self, model: &str, system: &str, user: String) -> Result<String, String> {
        let agent = self.env.agent;
        let parts = crate::llm::ask(&agent.pool, &agent.http, model, system, user).await.map_err(|e| e.message)?;
        Ok(parts.join(" "))
    }
}

/// `resolve_model(config.get("model"))`.
async fn resolve(env: &Env<'_>, model: Option<&Value>) -> Result<String, String> {
    crate::catalog::resolve_model(&env.agent.pool, model.and_then(Value::as_str)).await.map_err(|e| e.to_string())
}

/// `config.get(key, default)` as text: a string as is, anything else as
/// `str()` would print it; absent or null, the default.
pub fn text(config: &Map<String, Value>, key: &str, default: &str) -> String {
    match config.get(key) {
        None | Some(Value::Null) => default.to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(v) => pyjson::py_str(v),
    }
}

/// `int(config.get(key, default))`, or what `int()` raises.
fn int(config: &Map<String, Value>, key: &str, default: i64) -> Result<i64, String> {
    let Some(v) = config.get(key) else { return Ok(default) };
    pyjson::py_int(v).ok_or_else(|| match v {
        Value::String(s) => format!("invalid literal for int() with base 10: {}", pyjson::repr_str(s)),
        other => format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            pyjson::py_type(other)
        ),
    })
}

/// `'x' object has no attribute 'get'` — what a non-dict step or branch raises.
fn as_dict<'v>(v: &'v Value) -> Result<&'v Map<String, Value>, String> {
    v.as_object().ok_or_else(|| format!("'{}' object has no attribute 'get'", pyjson::py_type(v)))
}

/// The output port a config names: `config.get(key, default)` as a dict key.
fn port(config: &Map<String, Value>, key: &str, default: &str) -> String {
    text(config, key, default)
}

const KNOWN: &[&str] =
    &["agent", "conditional", "map", "start", "router", "refine", "sequential", "parallel", "loop", "approval", "human_input", "planner", "plan"];

/// `build_node(...).execute(inputs, task_state)`.
pub async fn execute(ctx: &Ctx<'_, '_>, kind: &str, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    match kind {
        "agent" => agent(ctx, config, inputs).await,
        "conditional" => conditional(ctx, config, inputs).await,
        "map" => map(ctx, config, inputs).await,
        "start" => start(config, inputs),
        "router" => router(ctx, config, inputs).await,
        "refine" => refine(ctx, config, inputs).await,
        "sequential" => sequential(ctx, config, inputs).await,
        "parallel" => parallel(ctx, config, inputs).await,
        "loop" => looped(ctx, config, inputs).await,
        "approval" => approval(ctx, config, inputs).await,
        "human_input" => human_input(ctx, config, inputs).await,
        "planner" | "plan" => planner(ctx, config, inputs).await,
        other => Err(format!(
            "Unknown node type: {}. Known types: [{}]",
            pyjson::repr_str(other),
            KNOWN.iter().map(|k| pyjson::repr_str(k)).collect::<Vec<_>>().join(", ")
        )),
    }
}

// ── structured output ───────────────────────────────────────────────────────

/// `_extract_first_json`: the whole text, else the longest prefix from the
/// first `{` or `[` that parses.
pub fn first_json(text: &str) -> Option<Value> {
    if text.is_empty() {
        return None;
    }
    if let Ok(v) = serde_json::from_str(text) {
        return Some(v);
    }
    let start = match (text.find('{'), text.find('[')) {
        (Some(a), Some(b)) => a.min(b),
        (a, b) => a.or(b)?,
    };
    let tail = &text[start..];
    let ends: Vec<usize> = tail.char_indices().map(|(i, c)| i + c.len_utf8()).collect();
    ends.into_iter().rev().find_map(|end| serde_json::from_str(&tail[..end]).ok())
}

/// `_maybe_parse_structured_output`, for a schema that's given: the JSON
/// found, or why none was. (The schema itself isn't checked here; Python
/// validates with `jsonschema` and adds a warning when it doesn't match.)
fn structured(text: &str) -> Result<Value, &'static str> {
    first_json(text).ok_or("Failed to extract JSON matching output_schema")
}

fn container(v: &Value) -> bool {
    v.is_object() || v.is_array()
}

/// The schema as the prompt shows it.
fn schema_text(schema: &Value) -> String {
    match schema {
        Value::String(s) => s.clone(),
        other => pyjson::dumps_indent(other, 2),
    }
}

fn kind_name(v: &Value) -> &'static str {
    if v.is_object() { "dict" } else { "list" }
}

// ── the nodes ───────────────────────────────────────────────────────────────

async fn agent(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let mut prompt = ctx.render(&text(config, "prompt_template", ""), inputs);
    let model = ctx.model(config).await?;
    let key = port(config, "output_key", "result");
    let schema = config.get("output_schema").filter(|s| pyjson::truthy(s));
    let strict = text(config, "output_schema_mode", "auto") == "strict";
    if let Some(schema) = schema {
        prompt = format!(
            "{prompt}\n\nYou must output valid JSON matching this JSON schema (or shape):\n{}\n\nOutput ONLY the JSON, no \
             extra explanation, no markdown fences.",
            schema_text(schema)
        );
    }
    let answer = run_text(ctx.env, ctx.node_id, &model, prompt).await?;
    let mut data = Map::new();
    if schema.is_none() {
        data.insert(key, answer.into());
        return Ok(Output::data(data));
    }
    match structured(&answer) {
        Ok(parsed) if container(&parsed) => {
            data.insert(key.clone(), parsed.clone());
            data.insert(format!("{key}_raw"), answer.into());
            if let Value::Object(m) = &parsed {
                for (k, v) in m {
                    if !data.contains_key(k) {
                        data.insert(k.clone(), v.clone());
                    }
                }
            }
            ctx.token(ctx.node_id, &format!("\n— structured output parsed ({}) —\n", kind_name(&parsed)));
            Ok(Output::data(data))
        }
        found => {
            if strict {
                return Err(format!(
                    "AgentNode {}: structured output required but no JSON found. Raw: {}",
                    ctx.node_id,
                    pystr::prefix(&answer, 500)
                ));
            }
            data.insert(key.clone(), answer.into());
            data.insert(format!("{key}_parse_error"), found.err().unwrap_or("no json").into());
            Ok(Output::data(data))
        }
    }
}

async fn conditional(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let condition = ctx.render(&text(config, "condition", ""), inputs);
    let model = ctx.model(config).await?;
    let raw = ctx
        .ask(&model, "You are a routing assistant. Answer ONLY with 'true' or 'false'. No explanation.", condition)
        .await?;
    let handle = if pystr::strip(&raw).to_lowercase().starts_with("true") { "true" } else { "false" };
    ctx.env.emit("node_condition", json!({"node_id": ctx.node_id, "verdict": handle}));
    Ok(Output { data: inputs.clone(), next: Some(vec![handle.into()]) })
}

/// The items a map runs over: a list's elements, a string's characters, a
/// dict's keys — whatever `enumerate()` gives.
fn items(v: &Value) -> Result<Vec<Value>, String> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::String(s) => Ok(s.chars().map(|c| Value::String(c.into())).collect()),
        Value::Object(m) => Ok(m.keys().map(|k| Value::String(k.clone())).collect()),
        other => Err(format!("object of type '{}' has no len()", pyjson::py_type(other))),
    }
}

async fn map(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let items_key = config.get("items_key").map(pyjson::py_str).ok_or("'items_key'")?;
    let result_key = port(config, "result_key", "results");
    let items = match inputs.get(&items_key) {
        None => vec![],
        Some(v) => items(v)?,
    };
    let definition = map_definition(ctx, config).await?;
    ctx.env.emit("map_start", json!({"node_id": ctx.node_id, "total": items.len()}));

    let child = ctx.env.item();
    let mut runs = Vec::with_capacity(items.len());
    for (index, item) in items.into_iter().enumerate() {
        runs.push(map_item(ctx, &child, &definition, inputs, index, item));
    }
    let results: Vec<Value> = match config.get("concurrency").filter(|c| pyjson::truthy(c)) {
        Some(_) => {
            let n = int(config, "concurrency", 0)?;
            futures_util::stream::iter(runs).buffered(n.max(1) as usize).try_collect().await?
        }
        None => futures_util::future::try_join_all(runs).await?,
    };
    let mut data = Map::new();
    data.insert(result_key, Value::Array(results));
    Ok(Output::data(data))
}

/// `run_item`: one item through the map's workflow, as a child.
async fn map_item(
    ctx: &Ctx<'_, '_>,
    child: &Env<'_>,
    definition: &Value,
    inputs: &Map<String, Value>,
    index: usize,
    item: Value,
) -> Result<Value, String> {
    let mut item_inputs = inputs.clone();
    item_inputs.insert("item".into(), item);
    item_inputs.insert("index".into(), index.into());
    let run_id = format!("{}_item_{index}", ctx.node_id);
    let outputs = match engine::execute(child, &run_id, definition, &item_inputs).await {
        Ok((outputs, _)) => outputs,
        Err(Halt::Failed(e)) => return Err(e),
        Err(Halt::Cancelled) => return Err("cancelled".to_string()),
    };
    ctx.env.emit("map_item_done", json!({"node_id": ctx.node_id, "index": index, "result": outputs}));
    Ok(Value::Object(outputs))
}

/// `MapNode._resolve_definition`: a saved workflow's, else the inline graph.
async fn map_definition(ctx: &Ctx<'_, '_>, config: &Map<String, Value>) -> Result<Value, String> {
    if let Some(id) = config.get("workflow_id").filter(|v| pyjson::truthy(v)) {
        let id = pyjson::py_str(id);
        let row: Option<String> = sqlx::query_scalar("SELECT definition FROM workflows WHERE id = ?")
            .bind(&id)
            .fetch_optional(&ctx.env.agent.pool)
            .await
            .map_err(|e| e.to_string())?;
        let definition = row.ok_or_else(|| format!("MapNode: workflow {} not found", pyjson::repr_str(&id)))?;
        return serde_json::from_str(&definition).map_err(|e| e.to_string());
    }
    match config.get("sub_graph").filter(|v| pyjson::truthy(v)) {
        Some(g) => Ok(g.clone()),
        None => Err("MapNode requires either 'workflow_id' or 'sub_graph' in config".into()),
    }
}

fn start(config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let mut data = match config.get("initial_inputs") {
        None => Map::new(),
        Some(Value::Object(m)) => m.clone(),
        Some(other) => return Err(format!("'{}' object is not a mapping", pyjson::py_type(other))),
    };
    data.extend(inputs.clone());
    Ok(Output::data(data))
}

/// `_match_category`: exact or prefix, then the longest name the answer
/// contains, then the first category.
pub fn match_category(response: &str, categories: &[String]) -> String {
    let text = pystr::strip(response).to_lowercase();
    if let Some(c) = categories.iter().find(|c| text == c.to_lowercase() || text.starts_with(&c.to_lowercase())) {
        return c.clone();
    }
    let mut longest: Vec<&String> = categories.iter().collect();
    // Stable, as `sorted(..., reverse=True)` keeps equal lengths in order.
    longest.sort_by_key(|c| std::cmp::Reverse(pystr::len(c)));
    longest.into_iter().find(|c| text.contains(&c.to_lowercase())).unwrap_or(&categories[0]).clone()
}

async fn router(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let categories: Vec<String> = match config.get("categories") {
        Some(Value::Array(a)) => a.iter().map(pyjson::py_str).collect(),
        _ => vec![],
    };
    if categories.is_empty() {
        return Err("RouterNode requires a non-empty 'categories' list".into());
    }
    let instruction = ctx.render(&text(config, "instruction", ""), inputs);
    let model = ctx.model(config).await?;
    let system = format!(
        "You are a routing classifier. Choose the single best-matching category for the input. Answer with ONLY the \
         category name, exactly as written, chosen from: {}. No explanation.",
        categories.join(", ")
    );
    let chosen = match_category(pystr::strip(&ctx.ask(&model, &system, instruction).await?), &categories);
    ctx.env.emit("node_condition", json!({"node_id": ctx.node_id, "verdict": chosen}));
    Ok(Output { data: inputs.clone(), next: Some(vec![chosen]) })
}

/// `_evaluate_draft`: PASS or FAIL as the first word, then the critique.
async fn evaluate(ctx: &Ctx<'_, '_>, model: &str, rubric: &str, task: &str, draft: &str) -> Result<(bool, String), String> {
    let criteria = if rubric.is_empty() { "(none given — judge overall quality and correctness)" } else { rubric };
    let text = ctx
        .ask(
            model,
            "You are a strict reviewer. Judge whether the draft satisfies the criteria. Respond with 'PASS' or 'FAIL' as \
             the very first word. If FAIL, follow it with specific, actionable critique of what to fix.",
            format!("Criteria:\n{criteria}\n\nOriginal task:\n{task}\n\nDraft:\n{draft}"),
        )
        .await?;
    let text = pystr::strip(&text).to_string();
    let low = text.to_lowercase();
    let passed = low.starts_with("pass");
    let critique = if low.starts_with("pass") || low.starts_with("fail") {
        text.chars().skip(4).collect::<String>().trim_start_matches([' ', ':', '.', '-', '\n']).to_string()
    } else {
        text
    };
    Ok((passed, critique))
}

/// The prompt for another round, after a reviewer's critique.
fn revision(base: &str, draft: &str, feedback: &str) -> String {
    format!(
        "{base}\n\nYour previous attempt:\n{draft}\n\nA reviewer judged it insufficient:\n{feedback}\n\nProduce an \
         improved version that fully addresses the critique."
    )
}

async fn refine(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let base = ctx.render(&text(config, "prompt_template", ""), inputs);
    let rubric = ctx.render(&text(config, "rubric", ""), inputs);
    let model = ctx.model(config).await?;
    let key = port(config, "output_key", "result");
    let max = int(config, "max_iterations", 3)?.clamp(1, 5);
    let (mut draft, mut feedback, mut passed, mut attempt) = (String::new(), String::new(), false, 0);
    for n in 1..=max {
        attempt = n;
        let prompt = if feedback.is_empty() {
            base.clone()
        } else {
            ctx.token(ctx.node_id, &format!("\n\n— revising (attempt {n}/{max}) —\n\n"));
            revision(&base, &draft, &feedback)
        };
        draft = run_text(ctx.env, ctx.node_id, &model, prompt).await?;
        (passed, feedback) = evaluate(ctx, &model, &rubric, &base, &draft).await?;
        ctx.token(ctx.node_id, &format!("\n\n— reviewer: {} —\n\n", if passed { "PASS" } else { "FAIL" }));
        if passed {
            break;
        }
    }
    let mut data = Map::new();
    data.insert(key.clone(), draft.into());
    data.insert(format!("{key}_iterations"), attempt.into());
    data.insert(format!("{key}_passed"), passed.into());
    Ok(Output::data(data))
}

async fn sequential(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let steps = match config.get("steps") {
        Some(Value::Array(s)) if !s.is_empty() => s.clone(),
        _ => return Err("SequentialNode requires non-empty 'steps' list".into()),
    };
    let default_model = ctx.model(config).await?;
    let mut data = inputs.clone();
    for (idx, step) in steps.iter().enumerate() {
        let step = as_dict(step)?;
        let key = port(step, "output_key", &format!("step_{idx}"));
        let model = match step.get("model").filter(|m| pyjson::truthy(m)) {
            Some(m) => resolve(ctx.env, Some(m)).await?,
            None => default_model.clone(),
        };
        let label = text(step, "label", &key);
        let schema = step.get("output_schema").filter(|s| pyjson::truthy(s));
        let mut prompt = ctx.render(&text(step, "prompt_template", ""), &data);
        if let Some(schema) = schema {
            prompt = format!("{prompt}\n\nYou must output valid JSON matching:\n{}\nOutput ONLY JSON.", schema_text(schema));
        }
        ctx.token(ctx.node_id, &format!("\n\n— sequential step {}/{}: {label} —\n\n", idx + 1, steps.len()));
        let result = run_text(ctx.env, &format!("{}_seq_{idx}", ctx.node_id), &model, prompt).await?;
        match schema.map(|_| structured(&result)) {
            Some(Ok(parsed)) if container(&parsed) => {
                data.insert(key, parsed.clone());
                if let Value::Object(m) = parsed {
                    for (k, v) in m {
                        if !data.contains_key(&k) {
                            data.insert(k, v);
                        }
                    }
                }
            }
            _ => {
                data.insert(key, result.into());
            }
        }
    }
    let final_schema = config.get("output_schema").filter(|s| pyjson::truthy(s));
    if let Some(key) = config.get("output_key").filter(|k| pyjson::truthy(k)) {
        let key = pyjson::py_str(key);
        let val = data.get(&key).cloned().unwrap_or_else(|| "".into());
        let mut out = Map::new();
        if let (Some(_), Value::String(s)) = (final_schema, &val) {
            match structured(s) {
                Ok(parsed) if container(&parsed) => {
                    out.insert(key.clone(), parsed);
                    out.insert(format!("{key}_raw"), val);
                    return Ok(Output::data(out));
                }
                _ => {}
            }
        }
        out.insert(key, val);
        return Ok(Output::data(out));
    }
    if final_schema.is_some() {
        let text = match data.get("result").filter(|v| pyjson::truthy(v)) {
            Some(v) => v.clone(),
            None => Value::String(pyjson::dumps(&Value::Object(data.clone()))),
        };
        if let Value::String(s) = &text {
            match structured(s) {
                Ok(Value::Object(m)) => return Ok(Output::data(m)),
                Ok(list @ Value::Array(_)) => {
                    let mut out = Map::new();
                    out.insert("result".into(), list);
                    return Ok(Output::data(out));
                }
                _ => {}
            }
        }
    }
    Ok(Output::data(data))
}

async fn parallel(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let branches = match config.get("branches") {
        Some(Value::Array(b)) if !b.is_empty() => b.clone(),
        _ => return Err("ParallelNode requires non-empty 'branches' list".into()),
    };
    let default_model = ctx.model(config).await?;
    ctx.env.emit("map_start", json!({"node_id": ctx.node_id, "total": branches.len()}));
    let mut runs = Vec::with_capacity(branches.len());
    for (idx, branch) in branches.iter().enumerate() {
        runs.push(branch_run(ctx, &default_model, inputs, idx, branch));
    }
    // Every branch runs to its end; then the first failure, if any, is the node's.
    let results: Vec<Result<(String, String), String>> = match config.get("concurrency").filter(|c| pyjson::truthy(c)) {
        Some(_) => {
            let n = int(config, "concurrency", 0)?;
            futures_util::stream::iter(runs).buffered(n.max(1) as usize).collect().await
        }
        None => futures_util::future::join_all(runs).await,
    };
    let mut data = inputs.clone();
    for r in results {
        let (key, value) = r?;
        data.insert(key, value.into());
    }
    Ok(Output::data(data))
}

/// `run_branch`: one branch's prompt through the agent.
async fn branch_run(
    ctx: &Ctx<'_, '_>,
    default_model: &str,
    inputs: &Map<String, Value>,
    idx: usize,
    branch: &Value,
) -> Result<(String, String), String> {
    let branch = as_dict(branch)?;
    let key = port(branch, "output_key", &format!("branch_{idx}"));
    let model = match branch.get("model").filter(|m| pyjson::truthy(m)) {
        Some(m) => resolve(ctx.env, Some(m)).await?,
        None => default_model.to_string(),
    };
    let label = branch.get("label").cloned().unwrap_or_else(|| Value::String(key.clone()));
    let prompt = ctx.render(&text(branch, "prompt_template", ""), inputs);
    let result = run_text(ctx.env, &format!("{}_par_{idx}", ctx.node_id), &model, prompt).await?;
    let mut shown = Map::new();
    shown.insert(key.clone(), result.clone().into());
    shown.insert("label".into(), label);
    ctx.env.emit("map_item_done", json!({"node_id": ctx.node_id, "index": idx, "result": shown}));
    Ok((key, result))
}

async fn looped(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let base = ctx.render(&text(config, "prompt_template", ""), inputs);
    let rubric = ctx.render(&text(config, "rubric", ""), inputs);
    let model = ctx.model(config).await?;
    let key = port(config, "output_key", "result");
    let max = int(config, "max_iterations", 3)?.clamp(1, 10);
    let (mut draft, mut feedback, mut passed, mut attempt) = (String::new(), String::new(), false, 0);
    for n in 1..=max {
        attempt = n;
        let prompt = if feedback.is_empty() {
            ctx.token(ctx.node_id, &format!("\n\n— loop start (attempt {n}/{max}) —\n\n"));
            base.clone()
        } else {
            ctx.token(ctx.node_id, &format!("\n\n— loop revising (attempt {n}/{max}) —\n\n"));
            revision(&base, &draft, &feedback)
        };
        draft = run_text(ctx.env, &format!("{}_loop_{n}", ctx.node_id), &model, prompt).await?;
        if rubric.is_empty() {
            ctx.token(ctx.node_id, &format!("\n\n— loop iteration {n}/{max} done (no rubric) —\n\n"));
            continue;
        }
        (passed, feedback) = evaluate(ctx, &model, &rubric, &base, &draft).await?;
        ctx.token(ctx.node_id, &format!("\n\n— reviewer: {} (attempt {n}) —\n\n", if passed { "PASS" } else { "FAIL" }));
        if passed {
            break;
        }
    }
    let mut data = Map::new();
    data.insert(key.clone(), draft.into());
    data.insert(format!("{key}_iterations"), attempt.into());
    data.insert(format!("{key}_passed"), (passed || rubric.is_empty()).into());
    Ok(Output::data(data))
}

/// `_extract_first_json` → a list of steps, else the lines of the reply
/// with their numbering stripped, else the reply itself.
fn plan_steps(raw: &str, max: usize) -> Vec<String> {
    let clean = |v: &Value| pystr::strip(&pyjson::py_str(v)).to_string();
    let mut steps: Vec<String> = match first_json(raw) {
        Some(Value::Array(a)) => a.iter().map(clean).filter(|s| !s.is_empty()).collect(),
        Some(Value::Object(m)) => ["steps", "plan", "tasks", "items"]
            .iter()
            .find_map(|k| m.get(*k).and_then(Value::as_array))
            .map(|a| a.iter().map(clean).filter(|s| !s.is_empty()).collect())
            .unwrap_or_default(),
        _ => vec![],
    };
    if steps.is_empty() {
        let lines: Vec<String> = pystr::strip(raw)
            .split('\n')
            .map(|l| l.strip_suffix('\r').unwrap_or(l))
            .map(|l| pystr::strip(l))
            .filter(|l| !l.is_empty())
            .map(|l| pystr::strip(strip_marker(l)).trim_matches(['"', '\'']).to_string())
            .filter(|l| pystr::len(l) > 5)
            .collect();
        steps = if lines.is_empty() { vec![pystr::prefix(raw, 500).to_string()] } else { lines };
    }
    steps.truncate(max);
    steps
}

/// `re.sub(r"^\s*(?:\d+[\.\)]\s*|[-*]\s+|step\s*\d+[:\.]\s*)", "", line, flags=re.I)`
/// on a line already stripped.
fn strip_marker(line: &str) -> &str {
    let digits = |s: &str| s.len() - s.trim_start_matches(|c: char| c.is_ascii_digit()).len();
    let n = digits(line);
    if n > 0 && matches!(line[n..].chars().next(), Some('.' | ')')) {
        return line[n + 1..].trim_start();
    }
    if let Some(rest) = line.strip_prefix(['-', '*']) {
        if rest.starts_with(char::is_whitespace) {
            return rest.trim_start();
        }
    }
    if line.len() >= 4 && line.is_char_boundary(4) && line[..4].eq_ignore_ascii_case("step") {
        let rest = line[4..].trim_start();
        let n = digits(rest);
        if n > 0 && matches!(rest[n..].chars().next(), Some(':' | '.')) {
            return rest[n + 1..].trim_start();
        }
    }
    line
}

async fn planner(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let goal = text(config, "goal", "");
    let base = ctx.render(&text(config, "prompt_template", &goal), inputs);
    let rubric = ctx.render(&text(config, "rubric", ""), inputs);
    let model = ctx.model(config).await?;
    let key = port(config, "output_key", "plan");
    let max = int(config, "max_steps", 5)?.clamp(1, 10) as usize;
    let schema = config.get("output_schema").filter(|s| pyjson::truthy(s));
    let mut instruction = format!(
        "You are a planning assistant. Given the goal, break it into exactly {max} or fewer concrete, actionable steps. \
         Each step should be a short sentence (10-15 words). Return ONLY a JSON array of strings — no explanation, no \
         markdown, no numbering inside strings."
    );
    if !rubric.is_empty() {
        instruction.push_str(&format!("\n\nGuidance / constraints:\n{rubric}"));
    }
    if let Some(schema) = schema {
        instruction.push_str(&format!("\n\nOutput must match JSON schema:\n{}", schema_text(schema)));
    }
    let user = if base.is_empty() { "Plan the steps to accomplish the inputs".to_string() } else { base };
    let raw = pystr::strip(&ctx.ask(&model, &instruction, user).await?).to_string();
    let parsed = schema.and_then(|_| structured(&raw).ok()).filter(container);
    let steps = if parsed.is_none() { plan_steps(&raw, max) } else { vec![] };
    for (i, step) in steps.iter().enumerate() {
        ctx.token(ctx.node_id, &format!("{}. {step}\n", i + 1));
    }
    let listed: Vec<String> = steps.iter().enumerate().map(|(i, s)| format!("{}. {s}", i + 1)).collect();
    let mut data = Map::new();
    data.insert(key.clone(), json!(steps));
    data.insert(format!("{key}_text"), listed.join("\n").into());
    data.insert("result".into(), json!(steps));
    if let Some(Value::Object(m)) = parsed {
        for (k, v) in m {
            if !data.contains_key(&k) {
                data.insert(k, v);
            }
        }
    }
    Ok(Output::data(data))
}

// ── pauses ──────────────────────────────────────────────────────────────────

/// How a human answered a paused node.
struct Answer {
    /// The row's status: `approved`, `denied` or `answered`.
    status: String,
    text: String,
}

/// `record_blocking_request` and the wait on it: the row filed, the run
/// marked paused, then the row polled until it's answered — `None` once
/// `timeout` passes, the row expired.
async fn pause(
    ctx: &Ctx<'_, '_>,
    kind: &str,
    question: &str,
    tool: &str,
    args_json: &str,
    timeout: Option<f64>,
) -> Result<Option<Answer>, String> {
    let env = ctx.env;
    let pool = &env.agent.pool;
    let (id, now) = (crate::gql::codec::new_id(), crate::gql::codec::now_stored());
    // A `run_workflow` call's workflow is nobody's run: Python files it the same way.
    let (source, label, task, parent) = match &env.pause {
        Some(p) => ("workflow", p.label.as_str(), Some(p.run_id.as_str()), Some(p.workflow_id.as_str())),
        None => ("chat", "", None, None),
    };
    sqlx::query(
        "INSERT INTO approvals (id, source, kind, status, question, label, tool, args_json, task_id, interrupt_id, \
         parent_id, requested_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    )
    .bind(&id)
    .bind(source)
    .bind(kind)
    .bind(question)
    .bind(label)
    .bind(tool)
    .bind(args_json)
    .bind(task)
    .bind(ctx.node_id)
    .bind(parent)
    .bind(&now)
    .bind(&now)
    .execute(pool)
    .await
    .map_err(|e| e.to_string())?;
    env.has_interrupt(true);
    let waited = wait(pool, &id, timeout).await;
    env.has_interrupt(false);
    waited
}

/// How often a paused node looks at its row.
const POLL: Duration = Duration::from_secs(1);

async fn wait(pool: &sqlx::SqlitePool, id: &str, timeout: Option<f64>) -> Result<Option<Answer>, String> {
    let deadline = timeout.and_then(|t| Duration::try_from_secs_f64(t).ok()).map(|t| tokio::time::Instant::now() + t);
    loop {
        let now = tokio::time::Instant::now();
        if deadline.is_some_and(|d| now >= d) {
            let stamp = crate::gql::codec::now_stored();
            sqlx::query(
                "UPDATE approvals SET status = 'expired', resolved_at = ?, updated_at = ?, \
                 result = 'No answer before the request timed out.' WHERE id = ? AND status = 'pending'",
            )
            .bind(&stamp)
            .bind(&stamp)
            .bind(id)
            .execute(pool)
            .await
            .map_err(|e| e.to_string())?;
            return Ok(None);
        }
        tokio::time::sleep(deadline.map_or(POLL, |d| POLL.min(d - now))).await;
        let row: Option<(String, Option<String>)> = sqlx::query_as("SELECT status, answer FROM approvals WHERE id = ?")
            .bind(id)
            .fetch_optional(pool)
            .await
            .map_err(|e| e.to_string())?;
        match row {
            Some((status, _)) if status == "pending" => {}
            Some((status, answer)) if ["approved", "denied", "answered"].contains(&status.as_str()) => {
                return Ok(Some(Answer { status, text: answer.unwrap_or_default() }));
            }
            Some((status, _)) => return Err(format!("the request was {status} before it was answered")),
            None => return Err("the request was deleted before it was answered".into()),
        }
    }
}

/// `timeout_seconds`, when truthy.
fn timeout(config: &Map<String, Value>) -> Result<Option<f64>, String> {
    match config.get("timeout_seconds").filter(|t| pyjson::truthy(t)) {
        None => Ok(None),
        Some(t) => pyjson::py_float(t)
            .map(Some)
            .ok_or_else(|| format!("could not convert {} to float: {}", pyjson::py_type(t), pyjson::py_repr(t))),
    }
}

async fn approval(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let reason = ctx.render(&text(config, "reason", "Approval required to continue"), inputs);
    let tool = text(config, "tool", ctx.node_id);
    let timeout = timeout(config)?;
    let on_deny = text(config, "on_deny", "error");
    let args = pyjson::dumps(&Value::Object(inputs.clone()));
    let id = ctx.node_id;
    ctx.env.emit("approval_request", json!({"tool": tool, "reason": reason, "args": args, "node_id": id}));
    ctx.env.emit("interrupt", json!({"interrupt_id": id, "question": format!("{tool}: {reason}")}));
    let answer = pause(ctx, "approval", &reason, &tool, pystr::prefix(&args, 2000), timeout).await?;
    let Some(answer) = answer else {
        ctx.env.emit("approval_resolved", json!({"tool": tool, "approved": false, "answer": "timeout", "node_id": id}));
        return Err(format!("Approval timed out for {tool}: {reason}"));
    };
    let approved = match answer.status.as_str() {
        "approved" => true,
        "denied" => false,
        _ => crate::approvals::is_affirmative(&answer.text) == Some(true),
    };
    ctx.env.emit("approval_resolved", json!({"tool": tool, "approved": approved, "answer": answer.text, "node_id": id}));
    ctx.env.emit("interrupt_resolved", json!({"interrupt_id": id}));
    let mut data = inputs.clone();
    data.insert("approved".into(), approved.into());
    data.insert("answer".into(), answer.text.clone().into());
    if approved {
        return Ok(Output { data, next: Some(vec!["approved".into()]) });
    }
    if on_deny == "continue" {
        return Ok(Output { data, next: Some(vec!["denied".into()]) });
    }
    Err(format!("Approval denied for {tool}: {reason} — answer: {}", answer.text))
}

async fn human_input(ctx: &Ctx<'_, '_>, config: &Map<String, Value>, inputs: &Map<String, Value>) -> Result<Output, String> {
    let asked = match config.get("prompt").or_else(|| config.get("question")) {
        Some(v) if pyjson::truthy(v) => pyjson::py_str(v),
        Some(_) => "Human input required".into(),
        None => "Human input required".into(),
    };
    let key = port(config, "output_key", "answer");
    let timeout = timeout(config)?;
    let prompt = ctx.render(&asked, inputs);
    let args = pyjson::dumps(&Value::Object(inputs.clone()));
    let id = ctx.node_id;
    ctx.env.emit("interrupt", json!({"interrupt_id": id, "question": prompt}));
    ctx.env.emit("approval_request", json!({"tool": id, "reason": prompt, "args": args, "node_id": id}));
    let answer = pause(ctx, "input", &prompt, id, pystr::prefix(&args, 2000), timeout).await?;
    let Some(answer) = answer else {
        ctx.env.emit("interrupt_resolved", json!({"interrupt_id": id}));
        return Err(format!("Human input timed out for {id}: {prompt}"));
    };
    ctx.env.emit("interrupt_resolved", json!({"interrupt_id": id}));
    ctx.env.emit("approval_resolved", json!({"tool": id, "approved": true, "answer": answer.text, "node_id": id}));
    let mut data = inputs.clone();
    data.insert(key, answer.text.clone().into());
    data.insert("answer".into(), answer.text.into());
    Ok(Output::data(data))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_json_is_pythons() {
        assert_eq!(first_json("[1, 2]"), Some(json!([1, 2])));
        assert_eq!(first_json("Sure: {\"a\": [1]} and more"), Some(json!({"a": [1]})));
        assert_eq!(first_json("x [1] {\"b\": 2}"), Some(json!([1])));
        assert_eq!(first_json("é {\"k\": \"ü\"}!"), Some(json!({"k": "ü"})));
        assert_eq!(first_json("no json"), None);
        assert_eq!(first_json("{broken"), None);
        assert_eq!(first_json("42"), Some(json!(42)));
    }

    #[test]
    fn categories_match_as_python_matches_them() {
        let cats: Vec<String> = ["bug", "bug report", "feature"].map(String::from).to_vec();
        assert_eq!(match_category("Bug", &cats), "bug");
        assert_eq!(match_category("  feature request", &cats), "feature");
        assert_eq!(match_category("this is a bug report.", &cats), "bug report");
        assert_eq!(match_category("unclear", &cats), "bug");
    }

    #[test]
    fn plans_are_read_as_python_reads_them() {
        assert_eq!(plan_steps("[\"a step\", \" \", 3]", 5), vec!["a step", "3"]);
        assert_eq!(plan_steps("{\"steps\": [\"x\", \"y\"]}", 1), vec!["x"]);
        assert_eq!(
            plan_steps("1. Gather the data\n- Clean it up please\r\nStep 3: \"Ship it now\"\nok", 5),
            vec!["Gather the data", "Clean it up please", "Ship it now"]
        );
        assert_eq!(plan_steps("tiny", 5), vec!["tiny"]);
    }

    #[test]
    fn markers_are_stripped_as_the_regex_strips_them() {
        assert_eq!(strip_marker("12) do it"), "do it");
        assert_eq!(strip_marker("* bullet"), "bullet");
        assert_eq!(strip_marker("*bold*"), "*bold*");
        assert_eq!(strip_marker("STEP 2. go"), "go");
        assert_eq!(strip_marker("Stepping out"), "Stepping out");
        assert_eq!(strip_marker("3 apples"), "3 apples");
    }

    #[test]
    fn int_raises_as_python_does() {
        let c = json!({"a": "x", "b": null, "c": 2.9, "d": "4"});
        let c = c.as_object().unwrap();
        assert_eq!(int(c, "a", 0).unwrap_err(), "invalid literal for int() with base 10: 'x'");
        assert!(int(c, "b", 0).unwrap_err().ends_with("not 'NoneType'"));
        assert_eq!((int(c, "c", 0), int(c, "d", 0), int(c, "z", 7)), (Ok(2), Ok(4), Ok(7)));
    }
}
