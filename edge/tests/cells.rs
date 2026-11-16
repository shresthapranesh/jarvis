//! `run_cell` on real kernels: a cell's own `timeout`, a stop in the middle
//! of a long cell, and a sub-agent's kernel reaped sooner than a chat's — the
//! sub-agent told its variables are gone when it is given work again.

mod support;

use std::time::{Duration, Instant};

use serde_json::{Value, json};
use support::{Edge, FakeModel, MODEL, Reply};

const START: &str = "mutation($input: StartTaskInput!) { startTask(input: $input) { taskId conversationId queued } }";

async fn edge() -> Edge {
    let edge = Edge::new().await;
    edge.fake_model_listed().await;
    edge.conversation("c", "Things", false).await;
    edge
}

/// Start a chat turn on `c`: its task id.
async fn begin(edge: &Edge, query: &str) -> String {
    let input = json!({"input": {"query": query, "model": MODEL, "conversationId": "c"}});
    let started = edge.graphql(START, input, &[]).await;
    started["data"]["startTask"]["taskId"].as_str().unwrap_or_else(|| panic!("{started}")).to_string()
}

/// Every tool result the model was sent, in the order it first saw them.
fn results(fake: &FakeModel) -> Vec<String> {
    let mut seen: Vec<String> = vec![];
    for request in fake.requests() {
        for m in request["messages"].as_array().unwrap() {
            let text = m["content"].as_str().unwrap_or_default().to_string();
            if m["role"] == "tool" && !seen.contains(&text) {
                seen.push(text);
            }
        }
    }
    seen
}

fn cell(code: &str, timeout: Option<i64>) -> Reply {
    match timeout {
        Some(t) => Reply::call("run_cell", json!({"code": code, "timeout": t})),
        None => Reply::call("run_cell", json!({"code": code})),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_cell_can_ask_for_more_time_and_is_told_how_when_it_runs_out() {
    let fake = FakeModel::start(vec![
        cell("import time\ntime.sleep(2)\n'slept'", Some(10)),
        cell("import time\ntime.sleep(10)", Some(1)),
        Reply::text("Done."),
    ])
    .await;
    let mut edge = edge().await;
    edge.start(&fake).await;
    begin(&edge, "sleep").await;
    edge.idle().await;

    let results = results(&fake);
    assert_eq!(results[0], "'slept'");
    assert!(results[1].contains("[execution timed out after 1s — kernel interrupted; session state is preserved]"), "{results:?}");
    assert!(
        results[1].ends_with("[Give longer work a larger `timeout` (at most 600s), or run it in the background with `jarvis.start_job(command)`.]"),
        "{results:?}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stop_interrupts_a_long_cell_at_once() {
    let started = std::env::temp_dir().join(format!("jarvis-cell-{}", uuid::Uuid::new_v4()));
    let long = format!("import pathlib, time\npathlib.Path({:?}).touch()\ntime.sleep(500)", started.display().to_string());
    let fake = FakeModel::start(vec![cell(&long, Some(600)), cell("'after'", None), Reply::text("Back.")]).await;
    let mut edge = edge().await;
    edge.start(&fake).await;
    let task = begin(&edge, "wait a long time").await;
    for _ in 0..600 {
        if started.exists() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(started.exists(), "the cell never started");
    let _ = std::fs::remove_file(&started);

    let at = Instant::now();
    let stopped = edge.graphql("mutation($id: String!) { stopTask(taskId: $id) }", json!({"id": task}), &[]).await;
    assert_eq!(stopped["data"]["stopTask"], true, "{stopped}");
    edge.idle().await;
    assert!(at.elapsed() < Duration::from_secs(10), "the stop took {:?}", at.elapsed());
    let reply = edge.rows("SELECT status FROM messages WHERE conversation_id = 'c' AND role = 'assistant'").await;
    assert_eq!(reply, [json!({"status": "stopped"})]);

    // The kernel was interrupted, not left sleeping: the next cell runs now.
    let at = Instant::now();
    begin(&edge, "and now?").await;
    edge.idle().await;
    assert!(at.elapsed() < Duration::from_secs(30), "the next cell waited {:?}", at.elapsed());
    assert!(results(&fake).iter().any(|r| r == "'after'"), "{:?}", results(&fake));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_sub_agent_s_kernel_goes_sooner_and_it_is_told_when_it_comes_back() {
    let task = |text: &str| Reply::call("run_agents", json!({"tasks": [{"task": text, "agent_id": "sub"}]}));
    let fake = FakeModel::start(vec![
        // The chat sets a variable, and gives its sub-agent a task…
        cell("y = 2", None),
        task("Set x."),
        cell("x = 1", None),
        Reply::text("Set."),
        Reply::text("It is set."),
        // …and later uses both again.
        cell("y", None),
        task("Show x."),
        cell("x", None),
        Reply::text("x is gone."),
        Reply::text("Done."),
    ])
    .await;
    let mut edge = edge().await;
    edge.execute(
        "INSERT INTO conversations (id, title, model, created_at, surface, pinned, ephemeral, parent_id) \
         VALUES ('sub', 'Helper', ?, '2026-10-04 00:00:00.000000', 'subagent', 0, 0, 'c')",
        &[MODEL],
    )
    .await;
    edge.env("JARVIS_SUBAGENT_KERNEL_IDLE_TIMEOUT", "1");
    edge.start(&fake).await;

    begin(&edge, "first").await;
    edge.idle().await;
    tokio::time::sleep(Duration::from_secs(3)).await;
    begin(&edge, "second").await;
    edge.idle().await;

    let results = results(&fake);
    // The chat's kernel kept its variables…
    assert!(results.iter().any(|r| r == "2"), "{results:?}");
    // …the sub-agent's was shut down, and the sub-agent is told so.
    let gone: Vec<&String> = results.iter().filter(|r| r.contains("NameError")).collect();
    assert_eq!(gone.len(), 1, "{results:?}");
    assert!(gone[0].starts_with("[This kernel was restarted since your last cell"), "{gone:?}");
    let answers: Vec<Value> = edge.rows("SELECT status FROM messages WHERE conversation_id = 'c' AND role = 'assistant'").await;
    assert_eq!(answers, [json!({"status": "done"}), json!({"status": "done"})]);
}
