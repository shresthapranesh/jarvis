//! The GraphQL schema: every query, mutation and subscription the frontend,
//! the bots and the `jarvis` SDK use.

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

use std::path::PathBuf;
use std::sync::Arc;

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

pub fn build(pool: SqlitePool, data: EdgeData, runs: Arc<crate::runs::Registry>) -> EdgeSchema {
    Schema::build(Query::default(), Mutation::default(), runs::RunSubscription)
        .data(pool)
        .data(data)
        .data(runs)
        .finish()
}
