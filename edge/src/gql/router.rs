//! Which server answers a GraphQL operation.
//!
//! Ownership is decided per *operation*, by its root fields: the edge takes an
//! operation only if it can answer all of it. Splitting one operation across
//! two servers would mean merging two partial results, and the two can't
//! share a transaction.
//!
//! A few root fields are owned only for some calls — see `Walk::field_rule`.

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

/// Root fields the edge's schema defines, per operation type.
#[derive(Default)]
pub struct Owned {
    pub query: HashSet<String>,
    pub mutation: HashSet<String>,
}

/// Fields that read or steer the live-run mirror (`runs.rs`), or start a run
/// for a worker to claim. The mirror is only current while a worker is
/// linked, and only a linked worker is woken for a new job, so without one
/// they're Python's — unless the edge owns the worker (`supervisor.rs`): then
/// no worker means no live run, and a new job starts one.
const LINKED_FIELDS: &[&str] = &[
    "runningTasks",
    "stopRunningTask",
    "stopTask",
    "stopAutomationRun",
    "stopWorkflowRun",
    "stopBoardTask",
    "startTask",
    "queueMessage",
    "unqueueMessage",
    "runWorkflow",
    "resumeWorkflowRun",
    "resolveWorkflowApproval",
    "triggerAutomation",
    "resolveApproval",
    "requestToolApproval",
];

/// Fields whose resolver may defer to Python (`gql::defer`), which runs the
/// whole operation again: owned only alone in it, so nothing an earlier
/// root field wrote is written twice.
const DEFERRING_FIELDS: &[&str] = &["resolveApproval", "requestToolApproval"];

/// Who sent the request. The `jarvis` SDK sends `X-Jarvis-Caller: agent`
/// (`server/graphql/context.py`); everything else is a human.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Caller {
    Human,
    Agent,
}

pub fn decide(
    owned: &Owned,
    query: &str,
    operation_name: Option<&str>,
    variables: &serde_json::Value,
    caller: Caller,
    link_up: bool,
) -> Decision {
    let doc = match parse_query(query) {
        Ok(doc) => doc,
        // Python owns the error message for a malformed document.
        Err(_) => return Decision::Backend("unparseable".into()),
    };
    let Some(op) = select_operation(&doc, operation_name) else {
        return Decision::Backend("operation not found".into());
    };
    let fields = match op.ty {
        OperationType::Query => &owned.query,
        OperationType::Mutation => &owned.mutation,
        // Over HTTP; the edge serves subscriptions on its WebSocket.
        OperationType::Subscription => return Decision::Backend("subscription".into()),
    };
    let walk = Walk { owned: fields, doc: &doc, variables, caller, link_up };
    let roots = walk.root_fields(&op.selection_set.node);
    if roots.len() > 1 {
        if let Some(name) = roots.iter().find(|n| DEFERRING_FIELDS.contains(&n.as_str())) {
            return Decision::Backend(format!("{name} beside other fields"));
        }
    }
    match walk.check(&op.selection_set.node) {
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

struct Walk<'a> {
    owned: &'a HashSet<String>,
    doc: &'a ExecutableDocument,
    variables: &'a serde_json::Value,
    caller: Caller,
    link_up: bool,
}

impl Walk<'_> {
    /// The root field names, through fragments.
    fn root_fields(&self, set: &SelectionSet) -> Vec<String> {
        let mut out = vec![];
        for item in &set.items {
            match &item.node {
                Selection::Field(field) if field.node.name.node != "__typename" => out.push(field.node.name.node.to_string()),
                Selection::Field(_) => {}
                Selection::InlineFragment(frag) => out.extend(self.root_fields(&frag.node.selection_set.node)),
                Selection::FragmentSpread(spread) => {
                    if let Some(frag) = self.doc.fragments.get(&spread.node.fragment_name.node) {
                        out.extend(self.root_fields(&frag.node.selection_set.node));
                    }
                }
            }
        }
        out
    }

    fn check(&self, set: &SelectionSet) -> Result<(), String> {
        for item in &set.items {
            match &item.node {
                Selection::Field(field) => {
                    let name = field.node.name.node.as_str();
                    if name != "__typename" && !self.owned.contains(name) {
                        return Err(format!("root field {name}"));
                    }
                    self.field_rule(name, &field.node)?;
                }
                Selection::InlineFragment(frag) => self.check(&frag.node.selection_set.node)?,
                Selection::FragmentSpread(spread) => {
                    let name = &spread.node.fragment_name.node;
                    let frag = self.doc.fragments.get(name).ok_or_else(|| format!("unknown fragment {name}"))?;
                    self.check(&frag.node.selection_set.node)?
                }
            }
        }
        Ok(())
    }

    /// Root fields the edge owns for only some calls.
    fn field_rule(&self, name: &str, field: &Field) -> Result<(), String> {
        if LINKED_FIELDS.contains(&name) && !self.link_up {
            return Err(format!("{name} needs a linked worker"));
        }
        match name {
            // Resolves any type, so owned per id: only types the edge implements.
            "node" => {
                let Some(serde_json::Value::String(id)) = self.argument(field, "id") else {
                    return Err("node id".into());
                };
                match decode_global_id(&id) {
                    Ok((ty, _)) if NODE_TYPES.contains(&ty.as_str()) => Ok(()),
                    Ok((ty, _)) => Err(format!("node type {ty}")),
                    Err(e) => Err(e),
                }
            }
            // An agent's delete may need a human's approval first
            // (`core/approvals.py:gate_action`); a human's click is the approval.
            "deleteWorkflow" | "deleteSkill" | "deleteAutomation" if self.caller == Caller::Agent => {
                Err(format!("{name} by the agent is approval-gated"))
            }
            // Python refuses a human's; its error is Python's to word.
            "requestToolApproval" if self.caller != Caller::Agent => Err("requestToolApproval by a human".into()),
            _ => Ok(()),
        }
    }

    /// An argument's value as JSON, through `$variables`. `None` when absent.
    fn argument(&self, field: &Field, name: &str) -> Option<serde_json::Value> {
        match &field.get_argument(name)?.node {
            Value::Variable(var) => self.variables.get(var.as_str()).cloned(),
            other => other.clone().into_const().and_then(|v| v.into_json().ok()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn owned() -> Owned {
        Owned {
            query: ["conversations", "conversation", "projects", "project", "node", "runningTasks"].map(String::from).into(),
            mutation: ["createProject", "updateConversation", "deleteWorkflow", "deleteSkill"].map(String::from).into(),
        }
    }

    fn decide_h(q: &str, vars: serde_json::Value) -> Decision {
        decide(&owned(), q, None, &vars, Caller::Human, true)
    }

    #[test]
    fn owned_query_goes_to_edge() {
        let q = "query ConversationListQuery { conversations { id title } }";
        assert_eq!(decide_h(q, json!({})), Decision::Edge);
    }

    #[test]
    fn mixed_query_goes_to_backend() {
        let q = "query { conversations { id } todos(conversationId: \"x\") { text } }";
        assert_eq!(decide_h(q, json!({})), Decision::Backend("root field todos".into()));
    }

    #[test]
    fn unowned_mutations_subscriptions_and_introspection_go_to_backend() {
        for q in [
            "mutation { startTask(input: {query: \"x\"}) { taskId } }",
            "mutation { createProject(input: {name: \"a\"}) { id } startTask(input: {query: \"x\"}) { taskId } }",
            "subscription { taskEvents(taskId: \"x\") { __typename } }",
            "{ __schema { types { name } } }",
        ] {
            assert!(matches!(decide_h(q, json!({})), Decision::Backend(_)), "{q}");
        }
    }

    #[test]
    fn owned_mutation_goes_to_edge() {
        assert_eq!(decide_h("mutation { createProject(input: {name: \"a\"}) { id } }", json!({})), Decision::Edge);
    }

    #[test]
    fn a_deferring_field_is_owned_only_alone() {
        let owned = Owned { query: Default::default(), mutation: ["createProject", "resolveApproval"].map(String::from).into() };
        let alone = "mutation { resolveApproval(id: \"a\", answer: \"y\") { id } }";
        let both = "mutation { createProject(input: {name: \"a\"}) { id } resolveApproval(id: \"a\", answer: \"y\") { id } }";
        assert_eq!(decide(&owned, alone, None, &json!({}), Caller::Human, true), Decision::Edge);
        assert!(matches!(decide(&owned, both, None, &json!({}), Caller::Human, true), Decision::Backend(_)));
    }

    #[test]
    fn agent_deletes_go_to_backend() {
        let q = "mutation { deleteWorkflow(id: \"x\") }";
        assert_eq!(decide(&owned(), q, None, &json!({}), Caller::Human, true), Decision::Edge);
        assert!(matches!(decide(&owned(), q, None, &json!({}), Caller::Agent, true), Decision::Backend(_)));
    }

    #[test]
    fn linked_fields_need_a_worker() {
        let q = "{ runningTasks { id } }";
        assert_eq!(decide(&owned(), q, None, &json!({}), Caller::Human, true), Decision::Edge);
        assert!(matches!(decide(&owned(), q, None, &json!({}), Caller::Human, false), Decision::Backend(_)));
    }

    #[test]
    fn node_routes_by_the_id_type() {
        let q = "query R($id: ID!) { node(id: $id) { __typename id } }";
        let conv = json!({"id": "Q29udmVyc2F0aW9uOmFiYw=="}); // Conversation:abc
        assert_eq!(decide_h(q, conv), Decision::Edge);
        let other = json!({"id": "UnVubmluZ1Rhc2s6YWJj"}); // RunningTask:abc
        assert_eq!(decide_h(q, other), Decision::Backend("node type RunningTask".into()));
    }

    #[test]
    fn root_fragments_are_followed() {
        let q = "query { ...F } fragment F on Query { conversations { id } todos(conversationId: \"x\") { text } }";
        assert!(matches!(decide_h(q, json!({})), Decision::Backend(_)));
    }

    #[test]
    fn named_operation_is_selected() {
        let q = "query A { conversations { id } } query B { todos(conversationId: \"x\") { text } }";
        assert_eq!(decide(&owned(), q, Some("A"), &json!({}), Caller::Human, true), Decision::Edge);
        assert!(matches!(decide(&owned(), q, Some("B"), &json!({}), Caller::Human, true), Decision::Backend(_)));
    }
}
