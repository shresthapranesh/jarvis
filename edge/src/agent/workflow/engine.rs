//! `workflow/engine.py:execute_workflow` — a change is made in both.
//!
//! The graph runs in level-synchronised frontiers: every ready node of a
//! level at once, the level's results applied in frontier order (so records
//! and outputs don't depend on which node finished first), then the nodes
//! they unblock. A conditional's unchosen edges are pruned, and a node all
//! of whose incoming edges are pruned never runs. Each node gets its
//! `timeout_seconds`, `retries` and `on_error` handling here.
//!
//! A stop ends the running nodes at once (Python lets an in-flight model
//! call finish first) and the run with them.

use std::collections::{HashSet, VecDeque};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use indexmap::IndexMap;
use serde_json::{Map, Value, json};

use super::nodes;
use super::template::Scope;
use crate::agent::Agent;
use crate::budget::Budget;
use crate::pyjson;
use crate::runs::Run;

/// What a node produced: its outputs by port, and — for a conditional, a
/// router or an approval — the one source handle whose edges stay active.
pub struct Output {
    pub data: Map<String, Value>,
    pub next: Option<Vec<String>>,
}

impl Output {
    pub fn data(data: Map<String, Value>) -> Self {
        Output { data, next: None }
    }
}

/// How a workflow stopped short of its outputs.
#[derive(Debug)]
pub enum Halt {
    /// Stopped by a human, or by its budget.
    Cancelled,
    /// The graph couldn't be run (Python raises out of `execute_workflow`).
    Failed(String),
}

/// Where a paused node's request is filed: the run a human answers, as
/// `record_blocking_request` names it.
#[derive(Clone)]
pub struct Pause {
    pub run_id: String,
    pub workflow_id: String,
    pub label: String,
}

/// The workflow's budget: every agent node's model and tool calls count
/// against it, as the run's tracker counted them in Python.
pub struct Meter {
    budget: Mutex<Budget>,
    run: Arc<Run>,
}

impl Meter {
    pub fn new(budget: Budget, run: Arc<Run>) -> Self {
        Meter { budget: Mutex::new(budget), run }
    }

    pub fn llm(&self, input: Option<i64>, output: Option<i64>) {
        self.record(|b| {
            let mut events = b.record_llm(input, output);
            events.extend(b.check());
            events
        });
    }

    pub fn tool(&self) {
        self.record(|b| b.record_tool(1));
    }

    pub fn exceeded(&self) -> Option<String> {
        self.budget.lock().expect("budget lock").exceeded().map(str::to_string)
    }

    fn record(&self, f: impl FnOnce(&mut Budget) -> Vec<crate::budget::Event>) {
        let (events, input, output, llm_calls, tool_calls, exceeded) = {
            let mut b = self.budget.lock().expect("budget lock");
            let events = f(&mut b);
            (events, b.input_tokens, b.output_tokens, b.llm_calls, b.tool_calls, b.exceeded().map(str::to_string))
        };
        for (event, data) in events {
            self.run.emit_local(event, &data);
        }
        self.run.update(|st| {
            let f = &mut st.fields;
            f.input_tokens = input;
            f.output_tokens = output;
            f.total_tokens = input + output;
            f.llm_calls = llm_calls;
            f.tool_calls = tool_calls;
            if let Some(reason) = exceeded {
                f.budget_exceeded = true;
                f.budget_reason = Some(reason);
                f.cancelled = true;
            }
        });
    }
}

/// What a workflow runs with.
pub struct Env<'a> {
    pub agent: &'a Agent,
    /// The run whose stream the events go to and whose stop stops this. A
    /// child workflow — a map's item, a `run_workflow` call — has none: in
    /// Python its `TaskState` is nobody's, so what it emits goes nowhere.
    pub run: Option<Arc<Run>>,
    /// Where an approval or input node files its request.
    pub pause: Option<Pause>,
    pub meter: Option<Arc<Meter>>,
    /// `run_workflow` calls this one is inside of (`_workflow_depth`).
    pub depth: u32,
}

impl Env<'_> {
    pub fn emit(&self, event: &str, data: Value) {
        if let Some(run) = &self.run {
            run.emit_local(event, &data);
        }
    }

    pub fn cancelled(&self) -> bool {
        self.run.as_ref().is_some_and(|r| r.fields().cancelled)
    }

    /// Resolves once the run is stopped; never for a child.
    pub async fn stopped(&self) {
        match &self.run {
            Some(run) => crate::agent::turn::until_stopped(run.clone()).await,
            None => std::future::pending().await,
        }
    }

    /// A map item's workflow: nothing reaches the run, but a paused node is
    /// still the run's to answer.
    pub fn item(&self) -> Env<'_> {
        Env { agent: self.agent, run: None, pause: self.pause.clone(), meter: None, depth: self.depth }
    }

    pub fn has_interrupt(&self, on: bool) {
        if let Some(run) = &self.run {
            run.update(|st| st.fields.has_interrupt = on);
        }
    }
}

// ── the graph ───────────────────────────────────────────────────────────────

struct Node {
    id: String,
    kind: String,
    label: Value,
    config: Map<String, Value>,
}

struct Edge {
    id: String,
    source: String,
    target: String,
    /// `sourceHandle` as given: an output port when it's a string.
    source_handle: Option<Value>,
    /// `str(edge.get("targetHandle", source_handle))`.
    target_handle: String,
}

impl Edge {
    /// The port a source output is read from: `edge.get("sourceHandle", "")`.
    fn reads(&self) -> Option<&str> {
        match &self.source_handle {
            None => Some(""),
            Some(Value::String(s)) => Some(s),
            Some(_) => None,
        }
    }
}

pub struct Graph {
    nodes: IndexMap<String, Node>,
    edges: Vec<Edge>,
}

/// `KeyError`'s message for a missing key.
fn key_error(key: &str) -> String {
    pyjson::repr_str(key)
}

fn id_of(v: &Value, key: &str) -> Result<String, String> {
    match v.get(key) {
        None => Err(key_error(key)),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(other) => Ok(pyjson::py_str(other)),
    }
}

impl Graph {
    pub fn parse(definition: &Value) -> Result<Graph, String> {
        let Value::Object(def) = definition else {
            return Err(format!("'{}' object has no attribute 'get'", pyjson::py_type(definition)));
        };
        let list = |key: &str| match def.get(key) {
            None => Ok(vec![]),
            Some(Value::Array(items)) => Ok(items.clone()),
            Some(other) => Err(format!("'{key}' must be a list, not {}", pyjson::py_type(other))),
        };
        let mut nodes = IndexMap::new();
        for n in list("nodes")? {
            let id = id_of(&n, "id")?;
            let config = match n.get("config") {
                Some(Value::Object(c)) => c.clone(),
                _ => Map::new(),
            };
            let kind = match n.get("type") {
                None => String::new(),
                Some(v) => pyjson::py_str(v),
            };
            let label = n.get("label").cloned().unwrap_or_else(|| Value::String(id.clone()));
            nodes.insert(id.clone(), Node { id, kind, label, config });
        }
        let mut edges = vec![];
        for e in list("edges")? {
            let (source, target) = (id_of(&e, "source")?, id_of(&e, "target")?);
            for end in [&source, &target] {
                if !nodes.contains_key(end) {
                    return Err(key_error(end));
                }
            }
            let source_handle = e.get("sourceHandle").cloned();
            let target_handle = match e.get("targetHandle").or(source_handle.as_ref()) {
                None => String::new(),
                Some(v) => pyjson::py_str(v),
            };
            edges.push(Edge { id: id_of(&e, "id")?, source, target, source_handle, target_handle });
        }
        Ok(Graph { nodes, edges })
    }

    /// `_is_ready`: every active incoming edge's source is done; a node
    /// whose incoming edges are all pruned is unreachable.
    fn ready(&self, id: &str, completed: &IndexMap<String, Map<String, Value>>, pruned: &HashSet<String>) -> bool {
        let incoming: Vec<&Edge> = self.edges.iter().filter(|e| e.target == id).collect();
        let active: Vec<&&Edge> = incoming.iter().filter(|e| !pruned.contains(&e.id)).collect();
        if !incoming.is_empty() && active.is_empty() {
            return false;
        }
        active.iter().all(|e| completed.contains_key(&e.source))
    }

    /// `_resolve_node_inputs`: each active incoming edge maps its source's
    /// output port onto this node's input port.
    fn inputs(&self, id: &str, completed: &IndexMap<String, Map<String, Value>>, pruned: &HashSet<String>) -> Map<String, Value> {
        let mut inputs = Map::new();
        for e in self.edges.iter().filter(|e| e.target == id && !pruned.contains(&e.id)) {
            let outputs = completed.get(&e.source);
            if let (Some(outputs), Some(port)) = (outputs, e.reads()) {
                if let Some(v) = outputs.get(port) {
                    inputs.insert(e.target_handle.clone(), v.clone());
                }
            }
        }
        inputs
    }
}

// ── running it ──────────────────────────────────────────────────────────────

pub type Ran = Result<(Map<String, Value>, Vec<Value>), Halt>;

/// `execute_workflow`: the terminal nodes' outputs merged, and a record of
/// every node that ran.
pub fn execute<'e>(env: &'e Env<'_>, run_id: &'e str, definition: &'e Value, inputs: &'e Map<String, Value>) -> Pin<Box<dyn Future<Output = Ran> + Send + 'e>> {
    Box::pin(async move {
        let graph = Graph::parse(definition).map_err(Halt::Failed)?;
        if graph.nodes.is_empty() {
            env.emit("workflow_error", json!({"error": "workflow has no nodes", "run_id": run_id}));
            return Ok((Map::new(), vec![]));
        }
        let targets: HashSet<&str> = graph.edges.iter().map(|e| e.target.as_str()).collect();
        let mut queue: VecDeque<String> = graph.nodes.keys().filter(|id| !targets.contains(id.as_str())).cloned().collect();
        let mut completed: IndexMap<String, Map<String, Value>> = IndexMap::new();
        let mut pruned: HashSet<String> = HashSet::new();
        let mut executed: HashSet<String> = HashSet::new();
        let mut records = vec![];

        while !queue.is_empty() {
            if env.cancelled() {
                return Err(Halt::Cancelled);
            }
            let mut frontier: Vec<String> = vec![];
            while let Some(id) = queue.pop_front() {
                if !executed.contains(&id) && !frontier.contains(&id) {
                    frontier.push(id);
                }
            }
            if frontier.is_empty() {
                break;
            }
            let scope = Scope { completed: &completed, workflow: inputs };
            let mut runs = Vec::with_capacity(frontier.len());
            for id in &frontier {
                runs.push(run_node(env, &graph, &graph.nodes[id.as_str()], scope, &pruned, inputs));
            }
            let batch = futures_util::future::join_all(runs).await;
            if env.cancelled() {
                return Err(Halt::Cancelled);
            }
            let mut done = vec![];
            for (id, result, record) in batch.into_iter().zip(&frontier).map(|((r, rec), id)| (id.clone(), r, rec)) {
                executed.insert(id.clone());
                records.push(record);
                let Some(out) = result else { continue };
                if let Some(next) = &out.next {
                    for e in graph.edges.iter().filter(|e| e.source == id) {
                        let active = matches!(&e.source_handle, Some(Value::String(h)) if next.contains(h));
                        if !active {
                            pruned.insert(e.id.clone());
                        }
                    }
                }
                completed.insert(id.clone(), out.data);
                done.push(id);
            }
            for id in done {
                for e in graph.edges.iter().filter(|e| e.source == id) {
                    let c = &e.target;
                    if !executed.contains(c) && !queue.contains(c) && graph.ready(c, &completed, &pruned) {
                        queue.push_back(c.clone());
                    }
                }
            }
        }

        let mut outputs = Map::new();
        for (id, out) in &completed {
            let terminal = !graph.edges.iter().any(|e| &e.source == id && !pruned.contains(&e.id));
            if terminal {
                outputs.extend(out.clone());
            }
        }
        env.emit("workflow_done", json!({"outputs": outputs, "run_id": run_id}));
        Ok((outputs, records))
    })
}

/// `_run_node`'s knobs, read as Python reads them.
struct Resilience {
    timeout: Option<f64>,
    retries: i64,
    delay: f64,
    on_error: String,
    fallback: Option<Value>,
}

impl Resilience {
    fn of(config: &Map<String, Value>) -> Self {
        let timeout = config
            .get("timeout_seconds")
            .or_else(|| config.get("timeout"))
            .filter(|v| !v.is_null())
            .and_then(pyjson::py_float)
            .filter(|t| *t > 0.0);
        let retries = config.get("retries").map_or(Some(0), pyjson::py_int).unwrap_or(0).clamp(0, 10);
        let delay = config
            .get("retry_delay_seconds")
            .or_else(|| config.get("retry_delay"))
            .map_or(Some(1.0), pyjson::py_float)
            .unwrap_or(1.0)
            .clamp(0.0, 60.0);
        let on_error = config.get("on_error").map_or_else(|| "error".to_string(), |v| pyjson::py_str(v).to_lowercase());
        let on_error = if ["error", "continue", "skip"].contains(&on_error.as_str()) { on_error } else { "error".into() };
        Resilience { timeout, retries, delay, on_error, fallback: config.get("fallback_output").cloned() }
    }
}

/// One node, with its retries, timeout and `on_error`: its output — `None`
/// when it failed and its branch stalls — and its record.
async fn run_node(
    env: &Env<'_>,
    graph: &Graph,
    node: &Node,
    scope: Scope<'_>,
    pruned: &HashSet<String>,
    workflow_inputs: &Map<String, Value>,
) -> (Option<Output>, Value) {
    let mut inputs = graph.inputs(&node.id, scope.completed, pruned);
    if inputs.is_empty() {
        inputs = workflow_inputs.clone();
    }
    let r = Resilience::of(&node.config);
    env.emit("node_start", json!({"node_id": node.id, "node_type": node.kind, "label": node.label}));

    let mut record = json!({
        "node_id": node.id,
        "node_type": node.kind,
        "label": node.label,
        "status": "running",
        "inputs": inputs,
        "outputs": null,
        "error": null,
        "started_at": crate::rest::isoformat_utc_now(),
        "finished_at": null,
        "attempts": 0,
        "timeout_seconds": r.timeout,
        "retries_config": r.retries,
        "on_error": r.on_error,
    });
    if node.kind == "agent" {
        let template = nodes::text(&node.config, "prompt_template", "");
        record["rendered_prompt"] = Value::String(super::template::render(&template, &inputs, scope));
    }

    let ctx = nodes::Ctx { env, node_id: &node.id, scope };
    let limit = r.timeout.and_then(|t| Duration::try_from_secs_f64(t).ok());
    let timed_out = format!("timeout after {}s", r.timeout.map(pyjson::float_repr).unwrap_or_default());
    let attempts = async {
        let mut last_error = String::new();
        for attempt in 0..=r.retries {
            record["attempts"] = json!(attempt + 1);
            let run = nodes::execute(&ctx, &node.kind, &node.config, &inputs);
            let ran = match limit {
                Some(limit) => match tokio::time::timeout(limit, run).await {
                    Ok(ran) => ran,
                    Err(_) => Err(timed_out.clone()),
                },
                None => run.await,
            };
            match ran {
                Ok(out) => {
                    record["status"] = "done".into();
                    record["outputs"] = Value::Object(out.data.clone());
                    record["finished_at"] = crate::rest::isoformat_utc_now().into();
                    if let Some(next) = &out.next {
                        record["verdict"] = next.first().cloned().into();
                    }
                    env.emit("node_done", json!({"node_id": node.id, "output": out.data}));
                    return Some(out);
                }
                Err(e) => last_error = e,
            }
            if attempt < r.retries {
                env.emit(
                    "node_retry",
                    json!({"node_id": node.id, "attempt": attempt + 1, "max_retries": r.retries, "error": last_error}),
                );
                tokio::time::sleep(Duration::from_secs_f64(r.delay)).await;
            }
        }
        let error = if last_error.is_empty() { "unknown error".to_string() } else { last_error };
        record["status"] = "error".into();
        record["error"] = error.clone().into();
        record["finished_at"] = crate::rest::isoformat_utc_now().into();
        env.emit("node_error", json!({"node_id": node.id, "error": error}));
        if r.on_error == "continue" || r.on_error == "skip" {
            let data = match &r.fallback {
                Some(Value::Object(m)) => m.clone(),
                _ => Map::new(),
            };
            record["status"] = "done".into();
            record["outputs"] = Value::Object(data.clone());
            record["fallback_used"] = true.into();
            env.emit("node_done", json!({"node_id": node.id, "output": data}));
            return Some(Output::data(data));
        }
        None
    };
    // Checked before each attempt too, as Python does.
    let out = if env.cancelled() {
        drop(attempts);
        Err(())
    } else {
        tokio::select! {
            out = attempts => Ok(out),
            () = env.stopped() => Err(()),
        }
    };
    match out {
        Ok(out) => (out, record),
        Err(()) => {
            record["status"] = "error".into();
            record["error"] = "cancelled".into();
            record["finished_at"] = crate::rest::isoformat_utc_now().into();
            env.emit("node_error", json!({"node_id": node.id, "error": "cancelled"}));
            (None, record)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edges_read_and_write_ports_as_python_does() {
        let g = Graph::parse(&json!({
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c", "label": 3}],
            "edges": [
                {"id": "e1", "source": "a", "target": "b", "sourceHandle": "out"},
                {"id": "e2", "source": "a", "target": "c", "sourceHandle": "x", "targetHandle": null},
                {"id": "e3", "source": "b", "target": "c"},
            ],
        }))
        .unwrap();
        assert_eq!(g.edges[0].target_handle, "out");
        assert_eq!(g.edges[1].target_handle, "None");
        assert_eq!(g.edges[2].reads(), Some(""));
        assert_eq!(g.nodes["c"].label, json!(3));
        assert_eq!(g.nodes["a"].label, json!("a"));
        let completed: IndexMap<String, Map<String, Value>> =
            [("a".to_string(), json!({"out": 1, "x": 2}).as_object().unwrap().clone())].into();
        let mut pruned = HashSet::new();
        assert_eq!(Value::Object(g.inputs("b", &completed, &pruned)), json!({"out": 1}));
        assert!(!g.ready("c", &completed, &pruned));
        pruned.insert("e3".to_string());
        assert!(g.ready("c", &completed, &pruned));
        pruned.insert("e2".to_string());
        assert!(!g.ready("c", &completed, &pruned), "every way in pruned");
        assert_eq!(Graph::parse(&json!({"nodes": [{"id": "a"}], "edges": [{"id": "e", "source": "a", "target": "z"}]})).err().unwrap(), "'z'");
        assert_eq!(Graph::parse(&json!([])).err().unwrap(), "'list' object has no attribute 'get'");
    }

    #[test]
    fn resilience_knobs_are_read_as_python_reads_them() {
        let r = Resilience::of(json!({"timeout": "2.5", "retries": 40, "retry_delay": -3, "on_error": "SKIP"}).as_object().unwrap());
        assert_eq!((r.timeout, r.retries, r.delay, r.on_error.as_str()), (Some(2.5), 10, 0.0, "skip"));
        let r = Resilience::of(json!({"timeout_seconds": 0, "retries": "x", "on_error": "explode"}).as_object().unwrap());
        assert_eq!((r.timeout, r.retries, r.delay, r.on_error.as_str()), (None, 0, 1.0, "error"));
    }
}
