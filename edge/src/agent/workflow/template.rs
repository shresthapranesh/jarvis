//! `core/workflow_template.py`: a node's `{{…}}` templates — Jinja first,
//! Python's regex renderer when Jinja refuses — a change is made in both.
//!
//! Jinja is minijinja here, set up to print values as Jinja prints Python
//! ones (`str()`: `None`, `True`, `{'k': 1}`) and with Python's `tojson` /
//! `json` / `fromjson` filters.

use std::sync::LazyLock;

use indexmap::IndexMap;
use minijinja::{Environment, Error, ErrorKind, Value as Jv};
use serde_json::{Map, Value};

use crate::pyjson;

/// What `{{nodes.*}}` and `{{workflow.*}}` resolve against: the outputs of
/// the nodes done so far, and the run's own inputs.
#[derive(Clone, Copy)]
pub struct Scope<'a> {
    pub completed: &'a IndexMap<String, Map<String, Value>>,
    pub workflow: &'a Map<String, Value>,
}

static JINJA: LazyLock<Environment<'static>> = LazyLock::new(|| {
    let mut env = Environment::new();
    env.set_formatter(|out, _state, value| {
        if value.is_undefined() {
            return Ok(());
        }
        let text = match value.as_str() {
            Some(s) => s.to_string(),
            None => match serde_json::to_value(value) {
                Ok(v) => pyjson::py_str(&v),
                Err(_) => value.to_string(),
            },
        };
        out.write_str(&text).map_err(|_| Error::new(ErrorKind::WriteFailure, "write failed"))
    });
    env.add_filter("tojson", to_json);
    env.add_filter("json", to_json);
    env.add_filter("fromjson", |v: Jv| -> Jv {
        match v.as_str().map(serde_json::from_str::<Value>) {
            Some(Ok(parsed)) => Jv::from_serialize(&parsed),
            _ => v,
        }
    });
    env
});

/// `_filter_json`: `json.dumps(s)`, or with `indent`.
fn to_json(v: Jv, indent: Option<Jv>) -> String {
    let value = serde_json::to_value(&v).unwrap_or(Value::Null);
    match indent.and_then(|i| i64::try_from(i).ok()) {
        Some(n) => pyjson::dumps_indent(&value, n.max(0) as usize),
        None => pyjson::dumps(&value),
    }
}

/// `render_template(template, inputs, completed, workflow_inputs)`.
pub fn render(template: &str, inputs: &Map<String, Value>, scope: Scope<'_>) -> String {
    if !template.contains("{{") {
        return template.to_string();
    }
    let mut ctx = inputs.clone();
    ctx.insert("inputs".into(), Value::Object(inputs.clone()));
    ctx.insert("nodes".into(), nodes(scope));
    ctx.insert("workflow".into(), Value::Object(scope.workflow.clone()));
    match JINJA.render_str(template, Value::Object(ctx)) {
        Ok(s) => s,
        // On a Jinja error the regex renderer, so a workflow doesn't break.
        Err(_) => fallback(template, inputs, scope),
    }
}

fn nodes(scope: Scope<'_>) -> Value {
    Value::Object(scope.completed.iter().map(|(k, v)| (k.clone(), Value::Object(v.clone()))).collect())
}

/// `_fallback_render`: each `{{ expr }}` looked up as a name or a dotted
/// path; a filter is ignored, and what isn't found stays as written.
pub fn fallback(template: &str, inputs: &Map<String, Value>, scope: Scope<'_>) -> String {
    let mut lookup = Map::new();
    lookup.insert("inputs".into(), Value::Object(inputs.clone()));
    lookup.insert("nodes".into(), nodes(scope));
    lookup.insert("workflow".into(), Value::Object(scope.workflow.clone()));
    for (k, v) in inputs {
        if !lookup.contains_key(k) {
            lookup.insert(k.clone(), v.clone());
        }
    }
    let replace = |expr: &str| -> Option<String> {
        let var = expr.split('|').next().unwrap_or("").trim().trim_matches(|c| c == '\'' || c == '"');
        if let Some(v) = lookup.get(var) {
            return Some(if v.is_null() { String::new() } else { pyjson::py_str(v) });
        }
        match dotted(var, &lookup) {
            Some(v @ Value::Object(_)) => Some(pyjson::dumps(v)),
            Some(v) => Some(pyjson::py_str(v)),
            None => None,
        }
    };
    substitute(template, replace)
}

/// `_resolve_dotted`; a null found counts as not found, as Python's `None` does.
fn dotted<'v>(path: &str, scope: &'v Map<String, Value>) -> Option<&'v Value> {
    let mut parts = path.split('.');
    let mut cur = scope.get(parts.next()?)?;
    for p in parts {
        cur = cur.as_object()?.get(p)?;
    }
    (!cur.is_null()).then_some(cur)
}

/// `re.sub(r"\{\{\s*(.+?)\s*\}\}", repl, template)`, `repl` given the
/// stripped expression; `None` keeps the match as written.
fn substitute(template: &str, mut repl: impl FnMut(&str) -> Option<String>) -> String {
    let mut out = String::with_capacity(template.len());
    let mut rest = template;
    while let Some(open) = rest.find("{{") {
        let after = &rest[open + 2..];
        let Some(close) = after.find("}}") else { break };
        let inner = after[..close].trim();
        if inner.is_empty() || inner.contains('\n') {
            // No match here: the regex moves on one character.
            let step = open + 1;
            out.push_str(&rest[..step]);
            rest = &rest[step..];
            continue;
        }
        out.push_str(&rest[..open]);
        match repl(inner) {
            Some(s) => out.push_str(&s),
            None => out.push_str(&rest[open..open + 2 + close + 2]),
        }
        rest = &after[close + 2..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn map(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn renders_as_jinja_renders_python_values() {
        let completed: IndexMap<String, Map<String, Value>> =
            [("n1".to_string(), map(json!({"result": "R", "n": 1.0, "d": {"a": [1, null, true]}})))].into();
        let wf = map(json!({"topic": "T"}));
        let scope = Scope { completed: &completed, workflow: &wf };
        let inputs = map(json!({"topic": "x", "items": ["a", "b"], "none": null}));
        let r = |t: &str| render(t, &inputs, scope);
        assert_eq!(r("plain"), "plain");
        assert_eq!(r("{{topic}} / {{ inputs.topic }} / {{workflow.topic}}"), "x / x / T");
        assert_eq!(r("{{ nodes.n1.result | upper }} {{ nodes.n1.n }}"), "R 1.0");
        assert_eq!(r("{{ nodes.n1.d }}"), "{'a': [1, None, True]}");
        assert_eq!(r("{{ items }} {{ none }} [{{ missing }}]"), "['a', 'b'] None []");
        assert_eq!(r("{{ missing | default('dflt') }}"), "dflt");
        assert_eq!(r("{{ nodes.n1.d | tojson }}"), r#"{"a": [1, null, true]}"#);
        assert_eq!(r("{{ '{\"k\": 2}' | fromjson }}"), "{'k': 2}");
        assert_eq!(r("{% for i in items %}{{ i }},{% endfor %}"), "a,b,");
        // A Jinja error falls back to the regex renderer.
        assert_eq!(r("{{ topic }} {{ nodes.nope.x }} {% bad"), "x {{ nodes.nope.x }} {% bad");
    }

    #[test]
    fn the_fallback_is_pythons() {
        let completed: IndexMap<String, Map<String, Value>> = [("n1".to_string(), map(json!({"r": "R", "z": null})))].into();
        let wf = Map::new();
        let scope = Scope { completed: &completed, workflow: &wf };
        let inputs = map(json!({"a": "A", "l": [1, "x"], "n": null}));
        let f = |t: &str| fallback(t, &inputs, scope);
        assert_eq!(f("{{a}}-{{ 'a' | upper }}-{{l}}-{{n}}"), "A-A-[1, 'x']-");
        assert_eq!(f("{{nodes.n1.r}} {{nodes.n1}} {{nodes.n1.z}} {{zz}}"), r#"R {"r": "R", "z": null} {{nodes.n1.z}} {{zz}}"#);
        assert_eq!(f("{{ }} {{\na}} {{a"), "{{ }} A {{a");
    }
}
