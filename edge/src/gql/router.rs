//! Which server answers a GraphQL operation.
//!
//! Ownership is decided per *operation*, by its root fields: the edge takes an
//! operation only if it can answer all of it. Splitting one operation across
//! two servers would mean merging two partial results, and the two can't
//! share a transaction.

use std::collections::HashSet;

use async_graphql::parser::parse_query;
use async_graphql::parser::types::{
    DocumentOperations, ExecutableDocument, Field, OperationDefinition, OperationType, Selection, SelectionSet,
};
use async_graphql_value::Value;

use super::codec::decode_global_id;
use super::node::NODE_TYPES;

#[derive(Debug, PartialEq, Eq)]
pub enum Decision {
    Edge,
    /// Why Python gets it — logged at debug, so a field that's expected to be
    /// ported but isn't is easy to spot.
    Backend(String),
}

pub fn decide(
    owned: &HashSet<String>,
    query: &str,
    operation_name: Option<&str>,
    variables: &serde_json::Value,
) -> Decision {
    let doc = match parse_query(query) {
        Ok(doc) => doc,
        // Python owns the error message for a malformed document.
        Err(_) => return Decision::Backend("unparseable".into()),
    };
    let Some(op) = select_operation(&doc, operation_name) else {
        return Decision::Backend("operation not found".into());
    };
    if op.ty != OperationType::Query {
        return Decision::Backend(format!("{:?}", op.ty).to_lowercase());
    }
    match check_selection(owned, &doc, &op.selection_set.node, variables) {
        Ok(()) => Decision::Edge,
        Err(why) => Decision::Backend(why),
    }
}

fn select_operation<'a>(doc: &'a ExecutableDocument, name: Option<&str>) -> Option<&'a OperationDefinition> {
    match (&doc.operations, name) {
        (DocumentOperations::Single(op), _) => Some(&op.node),
        (DocumentOperations::Multiple(ops), Some(name)) => ops.get(name).map(|op| &op.node),
        // The parser files a lone *named* operation under Multiple too.
        (DocumentOperations::Multiple(ops), None) if ops.len() == 1 => ops.values().next().map(|op| &op.node),
        (DocumentOperations::Multiple(_), None) => None,
    }
}

fn check_selection(
    owned: &HashSet<String>,
    doc: &ExecutableDocument,
    set: &SelectionSet,
    variables: &serde_json::Value,
) -> Result<(), String> {
    for item in &set.items {
        match &item.node {
            Selection::Field(field) => {
                let name = field.node.name.node.as_str();
                match name {
                    "__typename" => {}
                    "node" => owns_node_id(&field.node, variables)?,
                    _ if owned.contains(name) => {}
                    _ => return Err(format!("root field {name}")),
                }
            }
            Selection::InlineFragment(frag) => {
                check_selection(owned, doc, &frag.node.selection_set.node, variables)?
            }
            Selection::FragmentSpread(spread) => {
                let name = &spread.node.fragment_name.node;
                let frag = doc.fragments.get(name).ok_or_else(|| format!("unknown fragment {name}"))?;
                check_selection(owned, doc, &frag.node.selection_set.node, variables)?
            }
        }
    }
    Ok(())
}

/// `node(id:)` resolves any type, so it's owned per call: only when the id
/// names a type this schema implements.
fn owns_node_id(field: &Field, variables: &serde_json::Value) -> Result<(), String> {
    let id = match field.get_argument("id").map(|v| &v.node) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Variable(var)) => match variables.get(var.as_str()) {
            Some(serde_json::Value::String(s)) => s.clone(),
            _ => return Err("node id variable".into()),
        },
        _ => return Err("node id".into()),
    };
    match decode_global_id(&id) {
        Ok((ty, _)) if NODE_TYPES.contains(&ty.as_str()) => Ok(()),
        Ok((ty, _)) => Err(format!("node type {ty}")),
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn owned() -> HashSet<String> {
        ["conversations", "conversation", "projects", "project", "node"].map(String::from).into()
    }

    #[test]
    fn owned_query_goes_to_edge() {
        let q = "query ConversationListQuery { conversations { id title } }";
        assert_eq!(decide(&owned(), q, None, &json!({})), Decision::Edge);
    }

    #[test]
    fn mixed_query_goes_to_backend() {
        let q = "query { conversations { id } todos(conversationId: \"x\") { text } }";
        assert_eq!(decide(&owned(), q, None, &json!({})), Decision::Backend("root field todos".into()));
    }

    #[test]
    fn mutations_and_introspection_go_to_backend() {
        let m = "mutation { pinConversation(id: \"x\") { id } }";
        assert!(matches!(decide(&owned(), m, None, &json!({})), Decision::Backend(_)));
        let i = "{ __schema { types { name } } }";
        assert!(matches!(decide(&owned(), i, None, &json!({})), Decision::Backend(_)));
    }

    #[test]
    fn node_routes_by_the_id_type() {
        let q = "query R($id: ID!) { node(id: $id) { __typename id } }";
        let conv = json!({"id": "Q29udmVyc2F0aW9uOmFiYw=="}); // Conversation:abc
        assert_eq!(decide(&owned(), q, None, &conv), Decision::Edge);
        let wf = json!({"id": "V29ya2Zsb3c6YWJj"}); // Workflow:abc
        assert_eq!(decide(&owned(), q, None, &wf), Decision::Backend("node type Workflow".into()));
    }

    #[test]
    fn root_fragments_are_followed() {
        let q = "query { ...F } fragment F on Query { conversations { id } todos(conversationId: \"x\") { text } }";
        assert!(matches!(decide(&owned(), q, None, &json!({})), Decision::Backend(_)));
    }

    #[test]
    fn named_operation_is_selected() {
        let q = "query A { conversations { id } } query B { todos(conversationId: \"x\") { text } }";
        assert_eq!(decide(&owned(), q, Some("A"), &json!({})), Decision::Edge);
        assert!(matches!(decide(&owned(), q, Some("B"), &json!({})), Decision::Backend(_)));
    }
}
