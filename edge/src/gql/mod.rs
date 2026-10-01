//! The edge's GraphQL schema: the slice of the Python schema ported so far.
//!
//! It is deliberately partial. `router::decide` sends an operation here only
//! when every root field it selects is one this schema defines, and anything
//! the schema can't validate falls through to Python — so an un-ported field
//! means "served by Python", never "broken".

pub mod artifact;
pub mod automation;
pub mod board;
pub mod codec;
pub mod conversation;
pub mod memory;
pub mod node;
pub mod project;
pub mod router;
pub mod settings_lists;
pub mod workflow;
pub mod write;

use std::collections::HashSet;
use std::path::PathBuf;

use async_graphql::parser::parse_schema;
use async_graphql::parser::types::{TypeKind, TypeSystemDefinition};
use async_graphql::{EmptySubscription, MergedObject, Schema};
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
    memory::MemoryQuery,
    node::NodeQuery,
);

#[derive(MergedObject, Default)]
pub struct Mutation(
    conversation::ConversationMutation,
    project::ProjectMutation,
    artifact::ArtifactMutation,
    workflow::WorkflowMutation,
    settings_lists::ListsMutation,
    memory::MemoryMutation,
);

pub type EdgeSchema = Schema<Query, Mutation, EmptySubscription>;

/// Process-level facts resolvers need besides the pool.
pub struct EdgeData {
    pub artifacts_dir: PathBuf,
}

pub fn build(pool: SqlitePool, data: EdgeData) -> EdgeSchema {
    Schema::build(Query::default(), Mutation::default(), EmptySubscription)
        // Python serves introspection: it knows the whole schema, this one
        // only a slice of it. (The router also never sends `__schema` here.)
        .disable_introspection()
        .data(pool)
        .data(data)
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
