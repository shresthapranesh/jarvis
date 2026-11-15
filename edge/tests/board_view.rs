//! The board lists every top-level conversation — chats and board tasks —
//! in the column of what it is doing.

mod support;

use serde_json::{Value, json};
use support::{Edge, FakeModel, MODEL, Reply};

const NOW: &str = "2026-10-04 00:00:00.000000";
const BOARD: &str = "query($n: Int!) { board(doneLimit: $n) { doneTotal items { id state title conversationId reason \
                     task { status priority } } } }";
const START: &str = "mutation($input: StartTaskInput!) { startTask(input: $input) { taskId conversationId queued } }";

async fn board(edge: &Edge, done_limit: i64) -> Value {
    let body = edge.graphql(BOARD, json!({"n": done_limit}), &[]).await;
    assert!(body["errors"].is_null(), "{body}");
    body["data"]["board"].clone()
}

/// (id, state) of every item, in the order the board gives them.
fn states(board: &Value) -> Vec<(String, String)> {
    let items = board["items"].as_array().unwrap();
    items.iter().map(|i| (i["id"].as_str().unwrap().into(), i["state"].as_str().unwrap().into())).collect()
}

async fn message(edge: &Edge, conversation: &str, role: &str, content: &str, status: &str, at: &str) {
    let id = format!("{conversation}-{role}-{at}");
    edge.execute(
        "INSERT INTO messages (id, conversation_id, role, content, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        &[&id, conversation, role, content, status, at],
    )
    .await;
}

/// A board task as `createBoardTask` leaves one, then moved to `status`;
/// `ran` gives it the conversation its first run made.
async fn task(edge: &Edge, id: &str, status: &str, priority: &str, blocked: Option<(&str, &str)>, ran: bool) {
    edge.execute(
        &format!(
            "INSERT INTO board_tasks (id, title, status, priority, created_by, failure_count, created_at, updated_at) \
             VALUES (?, ?, ?, {priority}, 'user', 0, ?, ?)"
        ),
        &[id, &format!("Task {id}"), status, NOW, NOW],
    )
    .await;
    if let Some((reason, kind)) = blocked {
        edge.execute("UPDATE board_tasks SET blocked_reason = ?, blocked_kind = ? WHERE id = ?", &[reason, kind, id]).await;
    }
    if ran {
        edge.execute(
            "INSERT INTO conversations (id, title, model, created_at, surface, pinned, ephemeral) VALUES (?, ?, ?, ?, 'task', 0, 0)",
            &[&format!("boardtask_{id}"), &format!("Task {id}"), MODEL, NOW],
        )
        .await;
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn chats_and_tasks_stand_in_the_column_of_what_they_are_doing() {
    let fake = FakeModel::start(vec![]).await;
    let mut edge = Edge::new().await;
    edge.fake_model_listed().await;

    // Chats: answered and failed.
    edge.conversation("idle", "Idle chat", false).await;
    message(&edge, "idle", "assistant", "All done.", "done", "2026-10-04 00:00:01.000000").await;
    edge.conversation("failed", "Failed chat", false).await;
    message(&edge, "failed", "assistant", "the model refused", "error", "2026-10-04 00:00:02.000000").await;
    // Not on the board: incognito, a sub-agent's, an automation's.
    edge.conversation("secret", "Incognito", true).await;
    edge.execute(
        "INSERT INTO conversations (id, title, model, created_at, surface, pinned, ephemeral, parent_id) \
         VALUES ('sub', 'Sub-agent', ?, ?, 'subagent', 0, 0, 'idle')",
        &[MODEL, NOW],
    )
    .await;
    edge.execute(
        "INSERT INTO conversations (id, title, model, created_at, surface, pinned, ephemeral) \
         VALUES ('automation_x', 'Digest', ?, ?, 'automation', 0, 0)",
        &[MODEL, NOW],
    )
    .await;
    // Tasks: parked (two priorities), asking, failed, finished, archived.
    task(&edge, "low", "todo", "0", None, false).await;
    task(&edge, "high", "todo", "5", None, false).await;
    task(&edge, "ask", "blocked", "0", Some(("Which repo?", "needs_input")), true).await;
    task(&edge, "err", "blocked", "0", Some(("error: boom", "error")), true).await;
    task(&edge, "fin", "done", "0", None, true).await;
    task(&edge, "old", "archived", "0", None, true).await;
    edge.start(&fake).await;
    // A chat stopped on a question — after the start, whose sweep ends the
    // runs and questions a restart left behind.
    edge.conversation("asked", "Asking chat", false).await;
    message(&edge, "asked", "assistant", "", "running", "2026-10-04 00:00:03.000000").await;
    edge.execute(
        "INSERT INTO approvals (id, source, kind, status, question, label, parent_id, requested_at, updated_at) \
         VALUES ('q', 'ask', 'input', 'pending', 'Which one?', 'Question', 'asked', ?, ?)",
        &[NOW, NOW],
    )
    .await;

    let board = board(&edge, 20).await;
    let mut got = states(&board);
    // Todo is by priority; the rest are compared as sets of a column.
    assert_eq!(&got[..2], [("boardtask_high".into(), "todo".into()), ("boardtask_low".into(), "todo".into())]);
    got.sort();
    let mut want: Vec<(String, String)> = [
        ("asked", "needs_me"),
        ("boardtask_ask", "needs_me"),
        ("boardtask_err", "blocked"),
        ("boardtask_fin", "done"),
        ("boardtask_high", "todo"),
        ("boardtask_low", "todo"),
        ("failed", "blocked"),
        ("idle", "done"),
    ]
    .iter()
    .map(|(id, state)| (id.to_string(), state.to_string()))
    .collect();
    want.sort();
    assert_eq!(got, want);
    assert_eq!(board["doneTotal"], json!(2));

    let item = |id: &str| board["items"].as_array().unwrap().iter().find(|i| i["id"] == json!(id)).unwrap().clone();
    // What each waits on, and the task behind a task's conversation.
    assert_eq!(item("asked")["reason"], json!("Which one?"));
    assert_eq!(item("failed")["reason"], json!("the model refused"));
    assert_eq!(item("boardtask_ask")["reason"], json!("Which repo?"));
    assert_eq!(item("idle")["task"], Value::Null);
    assert_eq!(item("boardtask_high")["task"], json!({"status": "todo", "priority": 5}));
    // A task that has not run has no conversation to open.
    assert_eq!(item("boardtask_high")["conversationId"], Value::Null);
    assert_eq!(item("boardtask_fin")["conversationId"], json!("boardtask_fin"));

    // The task is reachable from its conversation, and only from its own.
    let query = "{ conversations(surface: null) { title boardTask { title status } } }";
    let listed = edge.graphql(query, json!({}), &[]).await;
    let listed = listed["data"]["conversations"].as_array().unwrap().clone();
    assert!(listed.contains(&json!({"title": "Task fin", "boardTask": {"title": "Task fin", "status": "done"}})), "{listed:?}");
    assert!(listed.contains(&json!({"title": "Idle chat", "boardTask": null})), "{listed:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn done_keeps_its_most_recent() {
    let fake = FakeModel::start(vec![]).await;
    let mut edge = Edge::new().await;
    edge.fake_model_listed().await;
    for n in 0..25 {
        let id = format!("chat{n:02}");
        edge.conversation(&id, &id, false).await;
        message(&edge, &id, "assistant", "ok", "done", &format!("2026-10-04 00:00:{n:02}.000000")).await;
    }
    edge.start(&fake).await;

    let cut = board(&edge, 20).await;
    assert_eq!(cut["doneTotal"], json!(25));
    let ids: Vec<String> = states(&cut).into_iter().map(|(id, _)| id).collect();
    let newest: Vec<String> = (5..25).rev().map(|n| format!("chat{n:02}")).collect();
    assert_eq!(ids, newest);

    let all = board(&edge, 100).await;
    assert_eq!(all["items"].as_array().unwrap().len(), 25);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_chat_is_working_while_its_turn_runs() {
    let fake = FakeModel::holding(vec![Reply::text("Hello.")], 0).await;
    let mut edge = Edge::new().await;
    edge.fake_model_listed().await;
    edge.start(&fake).await;

    let sent = edge.graphql(START, json!({"input": {"query": "hi", "model": MODEL}}), &[]).await;
    let conversation = sent["data"]["startTask"]["conversationId"].as_str().unwrap_or_else(|| panic!("{sent}")).to_string();
    fake.held().await;
    assert_eq!(states(&board(&edge, 20).await), [(conversation.clone(), "working".to_string())]);

    fake.release();
    edge.until("SELECT id FROM messages WHERE role = 'assistant' AND status = 'done'").await;
    edge.idle().await;
    assert_eq!(states(&board(&edge, 20).await), [(conversation, "done".to_string())]);
}
