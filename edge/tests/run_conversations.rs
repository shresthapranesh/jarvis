//! An automation's agent run and a workflow's agent node each answer in a
//! conversation — out of the sidebar's list, reachable from the run.

mod support;

use base64::Engine;
use serde_json::{Value, json};
use support::{Edge, FakeModel, MODEL, Reply};

const NOW: &str = "2026-10-04 00:00:00.000000";
const START: &str = "mutation($input: StartTaskInput!) { startTask(input: $input) { taskId conversationId queued } }";

fn gid(kind: &str, raw: &str) -> String {
    base64::engine::general_purpose::STANDARD.encode(format!("{kind}:{raw}"))
}

/// An edge with a prompt automation `auto` on the fake model.
async fn with_automation(stateful: bool) -> Edge {
    let edge = Edge::new().await;
    edge.fake_model_listed().await;
    edge.execute(
        "INSERT INTO automations (id, name, input_type, prompt_text, model, enabled, stateful, created_at, updated_at) \
         VALUES ('auto', 'Daily digest', 'prompt', 'Write the digest', ?, 1, ?, ?, ?)",
        &[MODEL, if stateful { "1" } else { "0" }, NOW, NOW],
    )
    .await;
    edge
}

/// One manual run of `auto`, finished: its id.
async fn trigger(edge: &Edge) -> String {
    let body = edge.graphql("mutation($id: ID!) { triggerAutomation(id: $id) }", json!({"id": gid("Automation", "auto")}), &[]).await;
    let run = body["data"]["triggerAutomation"].as_str().unwrap_or_else(|| panic!("{body}")).to_string();
    edge.until(&format!("SELECT id FROM automation_runs WHERE id = '{run}' AND status != 'running' AND status != 'pending'")).await;
    edge.idle().await;
    run
}

/// The titles of the conversations the sidebar lists.
async fn listed(edge: &Edge) -> Vec<Value> {
    let body = edge.graphql("{ conversations { title } }", json!({}), &[]).await;
    body["data"]["conversations"].as_array().unwrap().iter().map(|c| c["title"].clone()).collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn an_automation_run_has_a_conversation_of_its_own() {
    let fake = FakeModel::start(vec![Reply::text("Digest one."), Reply::text("Digest two."), Reply::text("Shorter.")]).await;
    let mut edge = with_automation(false).await;
    edge.start(&fake).await;

    let first = trigger(&edge).await;
    let second = trigger(&edge).await;

    // Each run: its conversation, the prompt, and the reply under the run's id.
    let conversations = edge.rows("SELECT id, title, surface, model FROM conversations ORDER BY rowid").await;
    assert_eq!(
        conversations,
        [
            json!({"id": format!("automation_{first}"), "title": "Daily digest", "surface": "automation", "model": MODEL}),
            json!({"id": format!("automation_{second}"), "title": "Daily digest", "surface": "automation", "model": MODEL}),
        ]
    );
    let messages = edge
        .rows(&format!(
            "SELECT id, role, content, status FROM messages WHERE conversation_id = 'automation_{first}' ORDER BY created_at, rowid"
        ))
        .await;
    assert_eq!(messages.len(), 2, "{messages:?}");
    assert_eq!((&messages[0]["role"], &messages[0]["content"]), (&json!("user"), &json!("Write the digest")));
    assert_eq!(messages[1], json!({"id": first, "role": "assistant", "content": "Digest one.", "status": "done"}));
    // The second run started fresh: it was not shown the first.
    assert_eq!(fake.requests()[1]["messages"].as_array().unwrap().iter().filter(|m| m["role"] == "user").count(), 1);

    // Out of the sidebar, reachable from the run.
    assert!(listed(&edge).await.is_empty());
    let runs = edge
        .graphql(
            "query($id: ID!) { automationRuns(automationId: $id) { output conversationId } }",
            json!({"id": gid("Automation", "auto")}),
            &[],
        )
        .await;
    let mut runs = runs["data"]["automationRuns"].as_array().unwrap().clone();
    runs.sort_by_key(|r| r["output"].as_str().unwrap().to_string());
    assert_eq!(
        runs,
        [
            json!({"output": "Digest one.", "conversationId": format!("automation_{first}")}),
            json!({"output": "Digest two.", "conversationId": format!("automation_{second}")}),
        ]
    );

    // It can be gone on with: the agent has the run behind it.
    let input = json!({"input": {"query": "make it shorter", "model": MODEL, "conversationId": format!("automation_{first}")}});
    let started = edge.graphql(START, input, &[]).await;
    assert!(started.get("errors").is_none(), "{started}");
    edge.idle().await;
    let asked: Vec<Value> =
        fake.requests()[2]["messages"].as_array().unwrap().iter().filter(|m| m["role"] != "system").cloned().collect();
    let said: Vec<(&str, &str)> = asked.iter().map(|m| (m["role"].as_str().unwrap(), m["content"].as_str().unwrap())).collect();
    assert_eq!(said, [("user", "Write the digest"), ("assistant", "Digest one."), ("user", "make it shorter")]);

    // The automation's conversations go with it.
    let deleted = edge.graphql("mutation($id: ID!) { deleteAutomation(id: $id) }", json!({"id": gid("Automation", "auto")}), &[]).await;
    assert_eq!(deleted["data"]["deleteAutomation"], json!(true), "{deleted}");
    assert!(edge.rows("SELECT id FROM conversations").await.is_empty());
    assert!(edge.rows("SELECT id FROM messages").await.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stateful_automations_runs_share_one_conversation() {
    let fake = FakeModel::start(vec![Reply::text("Baseline."), Reply::text("Nothing new.")]).await;
    let mut edge = with_automation(true).await;
    edge.start(&fake).await;

    let first = trigger(&edge).await;
    let second = trigger(&edge).await;

    assert_eq!(edge.rows("SELECT id FROM conversations").await, [json!({"id": "automation_auto"})]);
    let replies = edge.rows("SELECT id, content, status FROM messages WHERE role = 'assistant' ORDER BY created_at, rowid").await;
    assert_eq!(
        replies,
        [
            json!({"id": first, "content": "Baseline.", "status": "done"}),
            json!({"id": second, "content": "Nothing new.", "status": "done"}),
        ]
    );
    // The second run went on from the first.
    assert_eq!(fake.requests()[1]["messages"].as_array().unwrap().iter().filter(|m| m["role"] == "user").count(), 2);
    let runs = edge
        .graphql("query($id: ID!) { automationRuns(automationId: $id) { conversationId } }", json!({"id": gid("Automation", "auto")}), &[])
        .await;
    assert_eq!(runs["data"]["automationRuns"], json!([{"conversationId": "automation_auto"}, {"conversationId": "automation_auto"}]));
}

/// An edge with workflow `wf`, one agent node `draft`.
async fn with_workflow() -> Edge {
    let edge = Edge::new().await;
    edge.fake_model_listed().await;
    let definition = json!({
        "nodes": [{"id": "draft", "type": "agent", "label": "Draft", "config": {"prompt_template": "Draft the post", "model": MODEL}}],
        "edges": [],
    });
    edge.execute(
        "INSERT INTO workflows (id, name, definition, created_at, updated_at) VALUES ('wf', 'Blog post', ?, ?, ?)",
        &[&definition.to_string(), NOW, NOW],
    )
    .await;
    edge
}

async fn run_workflow(edge: &Edge) -> String {
    let body = edge.graphql("mutation($id: ID!) { runWorkflow(id: $id) }", json!({"id": gid("Workflow", "wf")}), &[]).await;
    body["data"]["runWorkflow"].as_str().unwrap_or_else(|| panic!("{body}")).to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_workflow_agents_reply_is_a_run_on_its_conversation() {
    // The agent's reply is held, so the test can look while it runs.
    let fake = FakeModel::holding(vec![Reply::text("A draft."), Reply::text("A longer draft.")], 0).await;
    let mut edge = with_workflow().await;
    edge.start(&fake).await;
    let run = run_workflow(&edge).await;
    fake.held().await;

    // Its reply is there, running, and is the run up on its conversation:
    let reply = edge.until("SELECT id, conversation_id FROM messages WHERE role = 'assistant' AND status = 'running'").await;
    let (reply, conversation) = (reply[0]["id"].clone(), reply[0]["conversation_id"].as_str().unwrap().to_string());
    let tasks = edge.graphql("{ runningTasks { id kind parentId done } }", json!({}), &[]).await;
    let tasks = tasks["data"]["runningTasks"].as_array().unwrap().clone();
    assert!(tasks.contains(&json!({"id": reply, "kind": "chat", "parentId": conversation, "done": false})), "{tasks:?}");
    // so a message sent there meanwhile joins it,
    let input = json!({"input": {"query": "make it longer", "model": MODEL, "conversationId": conversation}});
    let sent = edge.graphql(START, input, &[]).await;
    assert_eq!((&sent["data"]["startTask"]["taskId"], &sent["data"]["startTask"]["queued"]), (&reply, &json!(true)), "{sent}");

    fake.release();
    edge.until(&format!("SELECT id FROM workflow_runs WHERE id = '{run}' AND status = 'done'")).await;
    // and is the next turn once the agent has answered.
    edge.until("SELECT id FROM messages WHERE role = 'assistant' AND content = 'A longer draft.' AND status = 'done'").await;
    edge.idle().await;
    let messages = edge.rows("SELECT role, content, status FROM messages ORDER BY created_at, rowid").await;
    assert_eq!(
        messages,
        [
            json!({"role": "user", "content": "Draft the post", "status": "done"}),
            json!({"role": "assistant", "content": "A draft.", "status": "done"}),
            json!({"role": "user", "content": "make it longer", "status": "done"}),
            json!({"role": "assistant", "content": "A longer draft.", "status": "done"}),
        ]
    );
    // Its steps are the reply's rows, as a chat turn's are.
    let steps = edge.rows(&format!("SELECT node, seq FROM steps WHERE message_id = '{}'", reply.as_str().unwrap())).await;
    assert_eq!(steps, [json!({"node": "model_request", "seq": 0})]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stopped_workflow_stops_its_agents_reply() {
    let fake = FakeModel::holding(vec![Reply::text("A draft.")], 0).await;
    let mut edge = with_workflow().await;
    edge.start(&fake).await;
    let run = run_workflow(&edge).await;
    fake.held().await;
    edge.until("SELECT id FROM messages WHERE role = 'assistant' AND status = 'running'").await;

    let stopped = edge.graphql("mutation($id: String!) { stopWorkflowRun(runId: $id) }", json!({"id": run}), &[]).await;
    assert_eq!(stopped["data"]["stopWorkflowRun"], json!(true), "{stopped}");
    edge.until(&format!("SELECT id FROM workflow_runs WHERE id = '{run}' AND status = 'stopped'")).await;
    edge.until("SELECT id FROM messages WHERE role = 'assistant' AND status = 'stopped'").await;
    fake.release();
    let tasks = edge.graphql("{ runningTasks { id done } }", json!({}), &[]).await;
    assert!(tasks["data"]["runningTasks"].as_array().unwrap().iter().all(|t| t["done"] == json!(true)), "{tasks}");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_workflows_agent_node_has_a_conversation_of_its_own() {
    let fake = FakeModel::start(vec![Reply::text("A draft."), Reply::text("A longer draft.")]).await;
    let mut edge = with_workflow().await;
    edge.start(&fake).await;

    let run = run_workflow(&edge).await;
    edge.until(&format!("SELECT id FROM workflow_runs WHERE id = '{run}' AND status = 'done'")).await;
    edge.idle().await;

    let conversations = edge.rows("SELECT id, title, surface, model, spawned_by FROM conversations").await;
    let conversation = conversations[0]["id"].as_str().unwrap().to_string();
    assert_eq!(
        conversations,
        [json!({"id": conversation, "title": "Blog post · draft", "surface": "workflow", "model": MODEL, "spawned_by": run})]
    );
    let messages = edge.rows("SELECT role, content, status FROM messages ORDER BY created_at, rowid").await;
    assert_eq!(
        messages,
        [
            json!({"role": "user", "content": "Draft the post", "status": "done"}),
            json!({"role": "assistant", "content": "A draft.", "status": "done"}),
        ]
    );

    // Out of the sidebar, listed on the run.
    assert!(listed(&edge).await.is_empty());
    let found = edge
        .graphql(
            "query($id: ID!) { workflowRun(id: $id) { conversations { conversationId title status } } }",
            json!({"id": gid("WorkflowRun", &run)}),
            &[],
        )
        .await;
    assert_eq!(
        found["data"]["workflowRun"]["conversations"],
        json!([{"conversationId": conversation, "title": "Blog post · draft", "status": "done"}]),
        "{found}"
    );

    // It can be gone on with: the agent has what it said in the run behind it.
    let input = json!({"input": {"query": "make it longer", "model": MODEL, "conversationId": conversation}});
    let started = edge.graphql(START, input, &[]).await;
    assert!(started.get("errors").is_none(), "{started}");
    edge.idle().await;
    let asked: Vec<Value> =
        fake.requests()[1]["messages"].as_array().unwrap().iter().filter(|m| m["role"] != "system").cloned().collect();
    let said: Vec<(&str, &str)> = asked.iter().map(|m| (m["role"].as_str().unwrap(), m["content"].as_str().unwrap())).collect();
    assert_eq!(said, [("user", "Draft the post"), ("assistant", "A draft."), ("user", "make it longer")]);

    // The workflow's conversations go with it.
    let deleted = edge.graphql("mutation($id: ID!) { deleteWorkflow(id: $id) }", json!({"id": gid("Workflow", "wf")}), &[]).await;
    assert_eq!(deleted["data"]["deleteWorkflow"], json!(true), "{deleted}");
    assert!(edge.rows("SELECT id FROM conversations").await.is_empty());
}
