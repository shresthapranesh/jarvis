//! The images a cell shows, as the model is sent them: after the cell's text
//! in its `run_cell` result for a model that reads images — a few, of a
//! size, their bytes kept as blobs — and as a line of the text for one that
//! doesn't, including one the conversation moved to. Real kernels run the
//! cells (the checkout's `.venv`).

mod support;

use serde_json::{Value, json};
use support::{Edge, FakeModel, MODEL, Reply};

const START: &str = "mutation($input: StartTaskInput!) { startTask(input: $input) { taskId conversationId queued } }";

/// A 1×1 PNG.
const DOT: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

/// A cell that shows the dot `times` times, then prints.
fn dots(times: usize) -> Reply {
    let code = format!(
        "import base64\nfrom IPython.display import Image, display\nfor _ in range({times}):\n    \
         display(Image(data=base64.b64decode('{DOT}')))\nprint('drawn')"
    );
    Reply::call("run_cell", json!({"code": code}))
}

async fn edge() -> Edge {
    let edge = Edge::new().await;
    edge.fake_model_listed().await;
    edge.conversation("c", "Charts", false).await;
    edge
}

/// A chat turn on conversation `c`, run to its end.
async fn turn(edge: &Edge) {
    let input = json!({"input": {"query": "draw it", "model": MODEL, "conversationId": "c"}});
    let started = edge.graphql(START, input, &[]).await;
    assert!(started["data"]["startTask"]["taskId"].is_string(), "{started}");
    edge.idle().await;
}

/// The tool message of a request to the model.
fn result(request: &Value) -> Value {
    let messages = request["messages"].as_array().unwrap();
    let tools: Vec<&Value> = messages.iter().filter(|m| m["role"] == "tool").collect();
    assert_eq!(tools.len(), 1, "{messages:?}");
    tools[0].clone()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_model_that_reads_images_is_sent_the_cells() {
    let fake = FakeModel::seeing(vec![dots(1), Reply::text("A dot.")]).await;
    let mut edge = edge().await;
    edge.start(&fake).await;
    turn(&edge).await;

    let sent = result(&fake.requests()[1]);
    assert_eq!(sent["content"], "<IPython.core.display.Image object>[image/png output — image 1, shown after this text]drawn");
    assert_eq!(sent["images"], json!([DOT]));
    // The bytes are a blob the thread names, not base64 in its rows.
    let blobs = edge.rows("SELECT hash, mime_type, size FROM transcript_blobs").await;
    assert_eq!(blobs.len(), 1, "{blobs:?}");
    assert_eq!(blobs[0]["mime_type"], "image/png");
    let stored = edge.rows("SELECT data FROM thread_messages WHERE thread_id = 'c' AND role = 'tool'").await;
    let stored = stored[0]["data"].as_str().unwrap();
    assert!(stored.contains(blobs[0]["hash"].as_str().unwrap()) && !stored.contains(DOT), "{stored}");
    // The step the UI shows is the text.
    let step = edge.rows("SELECT data FROM steps WHERE node = 'tools'").await;
    let step: Value = serde_json::from_str(step[0]["data"].as_str().unwrap()).unwrap();
    assert_eq!(step, json!({"tool": "run_cell", "output": sent["content"]}));
    let reply = edge.rows("SELECT content FROM messages WHERE conversation_id = 'c' AND role = 'assistant'").await;
    assert_eq!(reply[0]["content"], "A dot.");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_model_that_does_not_is_told_in_text() {
    let fake = FakeModel::start(vec![dots(1), Reply::text("Something was drawn.")]).await;
    let mut edge = edge().await;
    edge.start(&fake).await;
    turn(&edge).await;

    let sent = result(&fake.requests()[1]);
    assert_eq!(sent["content"], "<IPython.core.display.Image object>[image/png output — not shown as text]drawn");
    assert!(sent.get("images").is_none(), "{sent}");
    assert!(edge.rows("SELECT hash FROM transcript_blobs").await.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_cell_sends_a_few_images_of_a_size() {
    let big = "from IPython.display import display\ndisplay({'image/png': 'A' * 2_200_000}, raw=True)\n";
    let code = format!("{big}{}", dots(5).calls[0].1["code"].as_str().unwrap());
    let fake = FakeModel::seeing(vec![Reply::call("run_cell", json!({"code": code})), Reply::text("Five dots.")]).await;
    let mut edge = edge().await;
    edge.start(&fake).await;
    turn(&edge).await;

    let sent = result(&fake.requests()[1]);
    let text = sent["content"].as_str().unwrap();
    assert!(text.starts_with("[image/png output — not shown: too large (1.6 MB; the limit is 1.5 MB)]"), "{text}");
    for n in 1..=4 {
        assert!(text.contains(&format!("[image/png output — image {n}, shown after this text]")), "{text}");
    }
    assert!(text.contains("[image/png output — not shown: a result shows at most 4 images]drawn"), "{text}");
    assert_eq!(sent["images"], json!([DOT, DOT, DOT, DOT]));
    // One picture, one blob.
    assert_eq!(edge.rows("SELECT hash FROM transcript_blobs").await.len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_conversation_moved_to_a_model_that_does_not_read_them_is_told_in_text() {
    let seeing = FakeModel::seeing(vec![dots(1), Reply::text("A dot.")]).await;
    let mut edge = edge().await;
    edge.start(&seeing).await;
    turn(&edge).await;
    assert_eq!(result(&seeing.requests()[1])["images"], json!([DOT]));

    // The same model id, served by one that no longer reads images.
    edge.stop();
    let blind = FakeModel::start(vec![Reply::text("Still a dot.")]).await;
    edge.start(&blind).await;
    turn(&edge).await;
    let sent = result(&blind.requests()[0]);
    assert_eq!(
        sent["content"],
        "<IPython.core.display.Image object>[image/png output — image 1, shown after this text]drawn\n\
         [image/png output — not shown: this model isn't sent images in tool results]"
    );
    assert!(sent.get("images").is_none(), "{sent}");
}
