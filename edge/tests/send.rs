//! `sendToConversation` (the SDK's `jarvis.send_message`): one conversation's
//! agent writes to another conversation, whose agent answers it there. One-way.

mod support;

use serde_json::{Value, json};
use support::{Edge, FakeModel, MODEL, Reply};

const SEND: &str = "mutation($c: String!, $m: String!) { sendToConversation(conversationId: $c, message: $m) \
                    { taskId conversationId queued } }";
const START: &str = "mutation($input: StartTaskInput!) { startTask(input: $input) { taskId conversationId queued } }";

const FROM_A: &str = "[Message from conversation a (Trip planning)]\nFlights came to 900.";

/// Two conversations, `a` and `b`.
async fn two() -> Edge {
    let edge = Edge::new().await;
    edge.fake_model_listed().await;
    edge.conversation("a", "Trip planning", false).await;
    edge.conversation("b", "Budget", false).await;
    edge
}

/// `sendToConversation` as the SDK calls it from `sender`'s kernel.
async fn send(edge: &Edge, sender: Option<&str>, to: &str, message: &str) -> Value {
    let mut headers = vec![("X-Jarvis-Caller", "agent")];
    if let Some(sender) = sender {
        headers.push(("X-Jarvis-Conversation", sender));
    }
    edge.graphql(SEND, json!({"c": to, "m": message}), &headers).await
}

#[tokio::test(flavor = "multi_thread")]
async fn a_message_from_another_conversation_starts_a_turn_there() {
    let fake = FakeModel::start(vec![Reply::text("Noted: 900.")]).await;
    let mut edge = two().await;
    edge.start(&fake).await;

    let body = send(&edge, Some("a"), "b", "  Flights came to 900.  ").await;
    assert!(body.get("errors").is_none(), "{body}");
    let sent = &body["data"]["sendToConversation"];
    assert_eq!((&sent["conversationId"], &sent["queued"]), (&json!("b"), &json!(false)));
    edge.idle().await;
    let job = edge.rows("SELECT id, kind, status, thread_id FROM jobs").await;
    assert_eq!(job, [json!({"id": sent["taskId"], "kind": "chat", "status": "done", "thread_id": "b"})]);

    // It is there as a message from the sender, which the agent there reads and answers.
    let messages =
        edge.rows("SELECT role, content, source, status FROM messages WHERE conversation_id = 'b' ORDER BY created_at, rowid").await;
    assert_eq!(
        messages,
        [
            json!({"role": "user", "content": FROM_A, "source": "conversation:a", "status": "done"}),
            json!({"role": "assistant", "content": "Noted: 900.", "source": null, "status": "done"}),
        ]
    );
    let asked = fake.requests();
    assert_eq!(asked[0]["messages"].as_array().unwrap().last(), Some(&json!({"role": "user", "content": FROM_A})));
    // Nothing comes back to the sender.
    assert!(edge.rows("SELECT id FROM messages WHERE conversation_id = 'a'").await.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_message_for_a_busy_conversation_joins_its_run() {
    // The first reply is held until the message has been sent.
    let fake = FakeModel::holding(vec![Reply::text("First."), Reply::text("And the flights.")], 0).await;
    let mut edge = two().await;
    edge.start(&fake).await;

    let input = json!({"input": {"query": "what's the budget?", "model": MODEL, "conversationId": "b"}});
    let started = edge.graphql(START, input, &[]).await;
    let running = &started["data"]["startTask"]["taskId"];
    assert!(running.is_string(), "{started}");
    fake.held().await;
    let body = send(&edge, Some("a"), "b", "Flights came to 900.").await;
    fake.release();
    assert_eq!(body["data"]["sendToConversation"], json!({"taskId": running, "conversationId": "b", "queued": true}));

    edge.until("SELECT id FROM messages WHERE conversation_id = 'b' AND source = 'conversation:a' AND status = 'done'").await;
    edge.idle().await;
    let read = fake.requests()[1..].iter().any(|request| {
        request["messages"].as_array().unwrap().iter().any(|m| m["role"] == "user" && m["content"].as_str().is_some_and(|c| c.contains(FROM_A)))
    });
    assert!(read, "the agent there never read it");
}

#[tokio::test(flavor = "multi_thread")]
async fn what_cannot_be_sent() {
    let fake = FakeModel::start(Vec::new()).await;
    let mut edge = two().await;
    edge.conversation("secret", "x", true).await;
    edge.start(&fake).await;

    for (sender, to, message, why) in [
        (None, "b", "hi", "only a conversation's agent can send to another conversation"),
        (Some("a"), "a", "hi", "a conversation can't send a message to itself"),
        (Some("a"), "nope", "hi", "conversation not found: nope"),
        (Some("a"), "secret", "hi", "conversation not found: secret"),
        (Some("a"), "b", "   ", "empty message"),
    ] {
        let body = send(&edge, sender, to, message).await;
        let errors: Vec<&Value> = body["errors"].as_array().unwrap().iter().map(|e| &e["message"]).collect();
        assert_eq!(errors, [&json!(why)], "{sender:?} -> {to}: {message:?}");
    }
    assert!(edge.rows("SELECT id FROM messages").await.is_empty());
    assert!(edge.rows("SELECT id FROM jobs").await.is_empty());
}
