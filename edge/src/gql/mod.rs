//! The edge's GraphQL schema: the slice of the Python schema ported so far.
//!
//! It is deliberately partial. `router::decide` sends an operation here only
//! when every root field it selects is one this schema defines, and anything
//! the schema can't validate falls through to Python — so an un-ported field
//! means "served by Python", never "broken".

pub mod codec;
pub mod conversation;
pub mod node;
pub mod project;
pub mod router;

use std::collections::HashSet;

use async_graphql::parser::parse_schema;
use async_graphql::parser::types::{TypeKind, TypeSystemDefinition};
use async_graphql::{EmptyMutation, EmptySubscription, MergedObject, Schema};
use sqlx::SqlitePool;

#[derive(MergedObject, Default)]
pub struct Query(conversation::ConversationQuery, project::ProjectQuery, node::NodeQuery);

pub type EdgeSchema = Schema<Query, EmptyMutation, EmptySubscription>;

pub fn build(pool: SqlitePool) -> EdgeSchema {
    Schema::build(Query::default(), EmptyMutation, EmptySubscription)
        // Python serves introspection: it knows the whole schema, this one
        // only a slice of it. (The router also never sends `__schema` here.)
        .disable_introspection()
        .data(pool)
        .finish()
}

/// Root query fields this schema defines, read back from its own SDL so the
/// routing table can't drift from what's actually implemented.
pub fn owned_root_fields(schema: &EdgeSchema) -> HashSet<String> {
    let doc = parse_schema(schema.sdl()).expect("the edge's own SDL parses");
    doc.definitions
        .into_iter()
        .find_map(|def| match def {
            TypeSystemDefinition::Type(t) if t.node.name.node == "Query" => match t.node.kind {
                TypeKind::Object(obj) => {
                    Some(obj.fields.into_iter().map(|f| f.node.name.node.to_string()).collect())
                }
                _ => None,
            },
            _ => None,
        })
        .unwrap_or_default()
}
