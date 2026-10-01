//! Relay's `node(id:)` — the refetch entrypoint `@refetchable` fragments use.
//!
//! The edge answers it only for the types listed in [`NODE_TYPES`]; for any
//! other id the router sends the operation to Python, which still knows every
//! type (see `router::owns_node_id`).

use async_graphql::{Context, ID, Interface, Object, Result};

use super::codec::decode_global_id;
use super::conversation::{Conversation, Message};
use super::project::Project;

/// Node types this schema can resolve. Keep in step with [`Node`].
pub const NODE_TYPES: &[&str] = &["Conversation", "Message", "Project"];

#[derive(Interface)]
#[graphql(field(name = "id", ty = "&ID", desc = "The Globally Unique ID of this object"))]
pub enum Node {
    Conversation(Conversation),
    Message(Message),
    Project(Project),
}

#[derive(Default)]
pub struct NodeQuery;

#[Object]
impl NodeQuery {
    async fn node(&self, ctx: &Context<'_>, #[graphql(desc = "The ID of the object.")] id: ID) -> Result<Node> {
        let (ty, raw) = decode_global_id(&id)?;
        let pool = ctx.data()?;
        let found = match ty.as_str() {
            "Conversation" => Conversation::by_id(pool, &raw).await?.map(Node::Conversation),
            "Message" => Message::by_id(pool, &raw).await?.map(Node::Message),
            "Project" => Project::by_id(pool, &raw).await?.map(Node::Project),
            other => return Err(format!("{other} is not a node type the edge serves").into()),
        };
        // Strawberry's `node` field is non-null and raises on a miss.
        found.ok_or_else(|| format!("{ty} {raw} not found").into())
    }
}
