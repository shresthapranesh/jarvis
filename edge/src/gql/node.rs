//! Relay's `node(id:)` — the refetch entrypoint `@refetchable` fragments use.
//!
//! The edge resolves the Node types in [`NODE_TYPES`]; for any other id the
//! router sends the operation to Python (`router::owns_node_id`).

use async_graphql::{Context, ID, Interface, Object, Result};
use sqlx::SqlitePool;

use super::artifact::{Artifact, Document};
use super::automation::AutomationRun;
use super::board::BoardTask;
use super::codec::decode_global_id;
use super::conversation::{Conversation, Message};
use super::project::Project;
use super::settings_lists::{NotificationChannel, Skill};
use super::workflow::{Workflow, WorkflowRun};

/// Node types this schema can resolve. Keep in step with [`Node`] and
/// [`resolve`].
pub const NODE_TYPES: &[&str] = &[
    "Artifact",
    "AutomationRun",
    "BoardTask",
    "Conversation",
    "Document",
    "Message",
    "NotificationChannel",
    "Project",
    "Skill",
    "Workflow",
    "WorkflowRun",
];

#[derive(Interface)]
#[graphql(field(name = "id", ty = "ID", desc = "The Globally Unique ID of this object"))]
pub enum Node {
    Artifact(Artifact),
    AutomationRun(AutomationRun),
    BoardTask(BoardTask),
    Conversation(Conversation),
    Document(Document),
    Message(Message),
    NotificationChannel(NotificationChannel),
    Project(Project),
    Skill(Skill),
    Workflow(Workflow),
    WorkflowRun(WorkflowRun),
}

async fn resolve(pool: &SqlitePool, ty: &str, raw: &str) -> Result<Option<Node>> {
    Ok(match ty {
        "Artifact" => Artifact::by_id(pool, raw).await?.map(Node::Artifact),
        "AutomationRun" => AutomationRun::by_id(pool, raw).await?.map(Node::AutomationRun),
        "BoardTask" => BoardTask::by_id(pool, raw).await?.map(Node::BoardTask),
        "Conversation" => Conversation::by_id(pool, raw).await?.map(Node::Conversation),
        "Document" => Document::by_id(pool, raw).await?.map(Node::Document),
        "Message" => Message::by_id(pool, raw).await?.map(Node::Message),
        "NotificationChannel" => NotificationChannel::by_id(pool, raw).await?.map(Node::NotificationChannel),
        "Project" => Project::by_id(pool, raw).await?.map(Node::Project),
        "Skill" => Skill::by_id(pool, raw).await?.map(Node::Skill),
        "Workflow" => Workflow::by_id(pool, raw).await?.map(Node::Workflow),
        "WorkflowRun" => WorkflowRun::by_id(pool, raw).await?.map(Node::WorkflowRun),
        other => return Err(format!("{other} is not a node type the edge serves").into()),
    })
}

#[derive(Default)]
pub struct NodeQuery;

#[Object]
impl NodeQuery {
    async fn node(&self, ctx: &Context<'_>, #[graphql(desc = "The ID of the object.")] id: ID) -> Result<Node> {
        let (ty, raw) = decode_global_id(&id)?;
        // Strawberry's `node` field is non-null and raises on a miss.
        resolve(ctx.data()?, &ty, &raw).await?.ok_or_else(|| format!("{ty} {raw} not found").into())
    }
}
