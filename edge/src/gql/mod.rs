//! The edge's GraphQL schema: the slice of the Python schema ported so far.
//!
//! It is deliberately partial. `router::decide` sends an operation here only
//! when every root field it selects is one this schema defines, so an
//! un-ported root field means "served by Python", never "broken". Every type
//! under one is ported whole (`tests/test_edge_parity.py` checks), so what
//! this schema can't validate is the request's error, answered here.

pub mod approval;
pub mod artifact;
pub mod automation;
pub mod board;
pub mod browser;
pub mod codec;
pub mod conversation;
pub mod events;
pub mod mcp;
pub mod memory;
pub mod model_sync;
pub mod models;
pub mod node;
pub mod project;
pub mod router;
pub mod runs;
pub mod settings;
pub mod settings_lists;
pub mod start;
pub mod tools;
pub mod workflow;
pub mod write;

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Arc;

use async_graphql::parser::parse_schema;
use async_graphql::parser::types::{TypeKind, TypeSystemDefinition};
use async_graphql::{MergedObject, Schema};
use sqlx::SqlitePool;

#[derive(MergedObject, Default)]
pub struct Query(
    conversation::ConversationQuery,
    project::ProjectQuery,
    artifact::ArtifactQuery,
    automation::AutomationQuery,
    board::BoardTaskQuery,
    workflow::WorkflowQuery,
    settings_lists::ListsQuery,
    settings::SettingQuery,
    memory::MemoryQuery,
    runs::RunQuery,
    models::ModelsQuery,
    model_sync::ModelSyncQuery,
    tools::ToolQuery,
    mcp::McpQuery,
    browser::BrowserQuery,
    node::NodeQuery,
);

#[derive(MergedObject, Default)]
pub struct Mutation(
    conversation::ConversationMutation,
    project::ProjectMutation,
    board::BoardTaskMutation,
    automation::AutomationMutation,
    approval::ApprovalMutation,
    artifact::ArtifactMutation,
    workflow::WorkflowMutation,
    settings_lists::ListsMutation,
    settings::SettingMutation,
    models::ModelsMutation,
    tools::ToolPolicyMutation,
    mcp::McpMutation,
    browser::BrowserMutation,
    memory::MemoryMutation,
    runs::RunMutation,
    start::StartMutation,
);

pub type EdgeSchema = Schema<Query, Mutation, runs::RunSubscription>;

/// Process-level facts resolvers need besides the pool.
pub struct EdgeData {
    pub artifacts_dir: PathBuf,
    /// The scheduler's zone, for `Automation.nextRunAt`.
    pub tz: chrono_tz::Tz,
    /// For `browserAvailable`'s probe.
    pub http: reqwest::Client,
    /// A board mutation that readies a card runs a dispatch pass at once.
    pub scheduler: Arc<crate::schedule::Scheduler>,
    /// A deleted conversation's notebook goes with it.
    pub kernels: Arc<crate::kernels::Kernels>,
    /// The MCP servers and their tools.
    pub mcp: Arc<crate::mcp::Mcp>,
}

/// Who sent the request, as `get_context` reads it: the `jarvis` SDK says
/// `X-Jarvis-Caller: agent` and names its conversation in
/// `X-Jarvis-Conversation`.
pub struct RequestFrom {
    pub caller: router::Caller,
    pub conversation: Option<String>,
}

/// The error extension that sends an operation to Python after all.
pub const DEFER: &str = "edgeDefer";

/// An error that makes `graphql::post` answer the operation in Python: the
/// data is there, but in a shape only Python reads (or rejects) faithfully.
/// Only before anything is written — the operation runs again in Python.
pub fn defer(why: String) -> async_graphql::Error {
    use async_graphql::ErrorExtensions;
    async_graphql::Error::new(why).extend_with(|_, e| e.set(DEFER, true))
}

pub fn build(pool: SqlitePool, data: EdgeData, runs: Arc<crate::runs::Registry>) -> EdgeSchema {
    Schema::build(Query::default(), Mutation::default(), runs::RunSubscription)
        // Python serves introspection: it knows the whole schema, this one
        // only a slice of it. (The router also never sends `__schema` here.)
        .disable_introspection()
        .data(pool)
        .data(data)
        .data(runs)
        .finish()
}

/// Root fields this schema defines, read back from its own SDL so the
/// routing table can't drift from what's actually implemented.
pub fn owned_root_fields(schema: &EdgeSchema) -> router::Owned {
    let doc = parse_schema(schema.sdl()).expect("the edge's own SDL parses");
    let fields_of = |type_name: &str| -> HashSet<String> {
        doc.definitions
            .iter()
            .find_map(|def| match def {
                TypeSystemDefinition::Type(t) if t.node.name.node == type_name => match &t.node.kind {
                    TypeKind::Object(obj) => Some(obj.fields.iter().map(|f| f.node.name.node.to_string()).collect()),
                    _ => None,
                },
                _ => None,
            })
            .unwrap_or_default()
    };
    router::Owned { query: fields_of("Query"), mutation: fields_of("Mutation") }
}
