//! What a run does when something goes wrong short of the model's answer:
//! a tool that fails on the server's side, a reply cut off at the output
//! limit, a budget that runs out. The model is told, and the run goes on or
//! closes with a last reply — it doesn't just end. And what a restart in the
//! middle of a run leaves of its limits.

mod support;

use serde_json::{Value, json};
use support::{Edge, FakeModel, MODEL, Reply};

const START: &str = "mutation($input: StartTaskInput!) { startTask(input: $input) { taskId conversationId queued } }";

/// A chat turn on conversation `c`, started.
async fn begin(edge: &Edge) {
    let input = json!({"input": {"query": "do the thing", "model": MODEL, "conversationId": "c"}});
    let started = edge.graphql(START, input, &[]).await;
    assert!(started["data"]["startTask"]["taskId"].is_string(), "{started}");
}

/// A chat turn on conversation `c`, run to its end: its reply's row.
async fn turn(edge: &Edge) -> Value {
    begin(edge).await;
    ended(edge).await
}

/// The reply's row of the turn on `c`, once it has ended.
async fn ended(edge: &Edge) -> Value {
    edge.idle().await;
    let reply = edge.rows("SELECT content, status FROM messages WHERE conversation_id = 'c' AND role = 'assistant'").await;
    assert_eq!(reply.len(), 1, "{reply:?}");
    reply[0].clone()
}

async fn edge() -> Edge {
    let edge = Edge::new().await;
    edge.fake_model_listed().await;
    edge.conversation("c", "Things", false).await;
    edge
}

/// The text of every message of `role` in a request to the model.
fn said(request: &Value, role: &str) -> Vec<String> {
    let messages = request["messages"].as_array().unwrap();
    messages.iter().filter(|m| m["role"] == role).map(|m| m["content"].as_str().unwrap_or_default().to_string()).collect()
}

fn todos() -> Reply {
    Reply::call("write_todos", json!({"todos": ["Do the thing"]}))
}

fn cell() -> Reply {
    Reply::call("run_cell", json!({"code": "1 + 1"}))
}

#[tokio::test(flavor = "multi_thread")]
async fn a_tool_that_fails_on_the_server_is_the_model_s_to_read() {
    let fake = FakeModel::start(vec![cell(), Reply::text("The kernel is down; here is what I know.")]).await;
    let mut edge = edge().await;
    // No kernel can start.
    edge.env("JARVIS_KERNEL_PYTHON", "/nonexistent/python");
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply, json!({"content": "The kernel is down; here is what I know.", "status": "done"}));
    let results = said(&fake.requests()[1], "tool");
    assert_eq!(results.len(), 1);
    assert!(results[0].starts_with("Error: run_cell failed: "), "{results:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn tools_that_keep_failing_fail_the_run() {
    let fake = FakeModel::start(vec![cell(), cell(), cell(), Reply::text("never asked for")]).await;
    let mut edge = edge().await;
    edge.env("JARVIS_KERNEL_PYTHON", "/nonexistent/python");
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply["status"], "error");
    let content = reply["content"].as_str().unwrap();
    assert!(content.contains("run_cell failed, the last of 3 tool failures in a row"), "{content}");
    assert_eq!(fake.requests().len(), 3);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_reply_cut_off_in_its_calls_is_asked_for_in_smaller_pieces() {
    let fake = FakeModel::start(vec![todos().cut(), Reply::text("Done, in two parts.")]).await;
    let mut edge = edge().await;
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply, json!({"content": "Done, in two parts.", "status": "done"}));
    // The call it was cut off in didn't run.
    let results = said(&fake.requests()[1], "tool");
    assert_eq!(results.len(), 1);
    assert!(results[0].starts_with("Not run: your reply reached the output limit"), "{results:?}");
    assert_eq!(edge.rows("SELECT todos FROM thread_state WHERE thread_id = 'c' AND todos LIKE '%Do the thing%'").await, Vec::<Value>::new());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_reply_cut_off_in_its_text_is_gone_on_with() {
    let fake = FakeModel::start(vec![Reply::text("The answer is").cut(), Reply::text("42.")]).await;
    let mut edge = edge().await;
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply, json!({"content": "The answer is 42.", "status": "done"}));
    let asked = said(&fake.requests()[1], "user");
    assert_eq!(asked.len(), 2, "{asked:?}");
    assert!(asked[1].starts_with("[Your reply was cut off at the output limit."), "{asked:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_second_cut_off_in_a_row_fails_the_run() {
    let fake = FakeModel::start(vec![todos().cut(), todos().cut(), Reply::text("never asked for")]).await;
    let mut edge = edge().await;
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply["status"], "error");
    assert!(reply["content"].as_str().unwrap().contains("output limit 2 times in a row"), "{reply}");
    assert_eq!(fake.requests().len(), 2);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_run_out_of_budget_says_where_it_got_to() {
    let fake = FakeModel::start(vec![todos(), todos(), Reply::text("Done: the plan. Left: the thing itself.")]).await;
    let mut edge = edge().await;
    edge.env("JARVIS_BUDGET_MAX_LLM_CALLS", "1");
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply["status"], "stopped");
    let content = reply["content"].as_str().unwrap();
    assert!(content.starts_with("Done: the plan. Left: the thing itself."), "{content}");
    assert!(content.contains("[Stopped: budget exceeded (llm calls 2 > limit 1)]"), "{content}");

    let requests = fake.requests();
    assert_eq!(requests.len(), 3);
    // Told it was running low while it could still act on it…
    assert!(!said(&requests[0], "system")[0].contains("## Running low"));
    assert!(said(&requests[1], "system")[0].contains("## Running low"));
    // …then that this is its last reply, the call the budget cut short answered.
    let closing = said(&requests[2], "system");
    assert!(closing[0].contains("## Wrap up now\n\nThis run reached its budget (llm calls 2 > limit 1)."), "{closing:?}");
    let results = said(&requests[2], "tool");
    assert_eq!(results.last().map(String::as_str), Some("Not run: this run reached its budget (llm calls 2 > limit 1)."));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_run_whose_answer_spent_its_budget_isn_t_asked_again() {
    let fake = FakeModel::start(vec![todos(), Reply::text("All done.")]).await;
    let mut edge = edge().await;
    edge.env("JARVIS_BUDGET_MAX_LLM_CALLS", "1");
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply["status"], "stopped");
    assert!(reply["content"].as_str().unwrap().starts_with("All done."), "{reply}");
    assert_eq!(fake.requests().len(), 2);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_run_claimed_again_goes_on_from_what_it_had_spent() {
    // The second call is under way when the server dies.
    let fake = FakeModel::holding(vec![todos(), Reply::text("lost with the server"), Reply::text("Finished.")], 1).await;
    let mut edge = edge().await;
    edge.env("JARVIS_BUDGET_MAX_LLM_CALLS", "1");
    edge.start(&fake).await;
    begin(&edge).await;
    fake.held().await;
    edge.stop();
    fake.release();
    edge.start(&fake).await;

    // One call before the restart and one after: over a budget of one.
    let reply = ended(&edge).await;
    assert_eq!(reply["status"], "stopped");
    let content = reply["content"].as_str().unwrap();
    assert!(content.contains("Finished.") && content.contains("budget exceeded (llm calls 2 > limit 1)"), "{content}");
    assert_eq!(edge.rows("SELECT attempts, status FROM jobs").await, [json!({"attempts": 2, "status": "done"})]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_run_interrupted_too_often_is_not_started_again() {
    let fake = FakeModel::holding(vec![Reply::text("lost with the server"), Reply::text("never asked for")], 0).await;
    let mut edge = edge().await;
    edge.start(&fake).await;
    begin(&edge).await;
    fake.held().await;
    edge.stop();
    fake.release();
    // As after three restarts, each of which interrupted it.
    edge.execute("UPDATE jobs SET attempts = max_attempts", &[]).await;
    edge.start(&fake).await;

    let reply = ended(&edge).await;
    assert_eq!(reply["status"], "error");
    assert!(reply["content"].as_str().unwrap().contains("the server restarted 3 times while this was running"), "{reply}");
    assert_eq!(edge.rows("SELECT attempts, status FROM jobs").await, [json!({"attempts": 4, "status": "error"})]);
    assert_eq!(fake.requests().len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_model_going_round_in_a_circle_is_told_so() {
    let fake = FakeModel::start(vec![todos(), todos(), todos(), Reply::text("I was stuck; here is where.")]).await;
    let mut edge = edge().await;
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply, json!({"content": "I was stuck; here is where.", "status": "done"}));
    let requests = fake.requests();
    // Twice is nothing; the third identical call with the identical result says so.
    let before = said(&requests[2], "tool");
    assert_eq!(before, ["Updated todo list (1 item).", "Updated todo list (1 item)."]);
    let after = said(&requests[3], "tool");
    assert!(after[2].starts_with("Updated todo list (1 item).\n\n[Note from the application: this is the same call"), "{after:?}");
    assert!(after[2].contains("3 times in a row"), "{after:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_sub_agent_works_under_its_project_s_instructions_and_knows_the_date() {
    let delegate = Reply::call("run_agents", json!({"tasks": [{"task": "Count the files."}]}));
    let fake = FakeModel::start(vec![delegate, Reply::text("Three."), Reply::text("There are three.")]).await;
    let mut edge = edge().await;
    edge.execute(
        "INSERT INTO projects (id, name, description, instructions, memory, created_at, updated_at) \
         VALUES ('p', 'Atlas', NULL, 'Always cite your sources.', '', '2026-10-04 00:00:00.000000', '2026-10-04 00:00:00.000000')",
        &[],
    )
    .await;
    edge.execute("UPDATE conversations SET project_id = 'p' WHERE id = 'c'", &[]).await;
    edge.start(&fake).await;

    let reply = turn(&edge).await;
    assert_eq!(reply, json!({"content": "There are three.", "status": "done"}));
    let requests = fake.requests();
    assert_eq!(requests.len(), 3);
    assert!(said(&requests[0], "system")[0].contains("\n\n## Today\n\n"));
    let sub_agent = &said(&requests[1], "system")[0];
    assert!(sub_agent.starts_with("You are a sub-agent"), "{sub_agent}");
    assert!(sub_agent.contains("## Project: Atlas") && sub_agent.contains("Always cite your sources."), "{sub_agent}");
    assert!(sub_agent.contains("\n\n## Today\n\n"), "{sub_agent}");
    // Not the main agent's project memory how-to.
    assert!(!sub_agent.contains("Project memory is a short shared summary"), "{sub_agent}");
}
