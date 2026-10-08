//! Reading a GraphQL request before it executes: who sent it, and the
//! operation it names.

use async_graphql::parser::parse_query;
use async_graphql::parser::types::DocumentOperations;

/// Who sent the request. The `jarvis` SDK sends `X-Jarvis-Caller: agent`;
/// everything else is a human.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Caller {
    Human,
    Agent,
}

/// Strawberry's 400 for an `operationName` that names no operation in the
/// document. Anything else — a parse error included — is executing's to
/// report.
pub fn unknown_operation(query: &str, operation_name: Option<&str>) -> Option<String> {
    let name = operation_name?;
    let doc = parse_query(query).ok()?;
    let found = match &doc.operations {
        DocumentOperations::Single(_) => false,
        DocumentOperations::Multiple(ops) => ops.contains_key(name),
    };
    (!found).then(|| format!("Unknown operation named \"{name}\"."))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_operation_name_must_name_an_operation() {
        let q = "query A { conversations { id } }";
        assert_eq!(unknown_operation(q, Some("A")), None);
        assert_eq!(unknown_operation(q, Some("B")), Some("Unknown operation named \"B\".".into()));
        assert!(unknown_operation("{ conversations { id } }", Some("A")).is_some());
        assert_eq!(unknown_operation("{ conversations ", Some("A")), None);
        assert_eq!(unknown_operation(q, None), None);
    }
}
