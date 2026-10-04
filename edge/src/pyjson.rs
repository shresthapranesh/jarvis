//! Python's value semantics, for coercing event payloads exactly as the
//! Python subscriptions did (`server/graphql/types/*events.py`).
//!
//! Several event fields embed JSON *text* — `StepEvent.data`, the budget and
//! perf `snapshot`s — produced by Python's `json.dumps`, so the edge must
//! produce the same bytes: `", "` / `": "` separators, every non-ASCII
//! character escaped, and floats in Python's `repr` form (`1e-05`, `1.0`).
//! Truthiness (`x or default`) and `str(x)` are here for the same reason.

use serde_json::Value;

/// `json.dumps(value)` with the default arguments.
pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, true);
    out
}

/// `json.dumps(value, ensure_ascii=False)` — how LangChain's OpenAI
/// integration writes tool-call arguments.
pub fn dumps_unicode(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, false);
    out
}

fn write_value(out: &mut String, value: &Value, ascii: bool) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&number_repr(n)),
        Value::String(s) => write_string(out, s, ascii),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_value(out, item, ascii);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_string(out, k, ascii);
                out.push_str(": ");
                write_value(out, v, ascii);
            }
            out.push('}');
        }
    }
}

/// `ensure_ascii=True`: everything outside printable ASCII becomes `\uXXXX`
/// (astral characters as a surrogate pair), DEL included. Without it, only
/// control characters are escaped.
fn write_string(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            c if !ascii && c >= ' ' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn number_repr(n: &serde_json::Number) -> String {
    if let Some(i) = n.as_i64() {
        i.to_string()
    } else if let Some(u) = n.as_u64() {
        u.to_string()
    } else {
        float_repr(n.as_f64().unwrap_or(f64::NAN))
    }
}

/// Python's `repr(float)`: the shortest round-tripping digits, written fixed
/// when the decimal exponent is in [-4, 16) and scientific otherwise.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "NaN".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    // `{:e}` gives the same shortest digits Python uses, as d.ddde±n.
    let sci = format!("{x:e}");
    let (mantissa, exp) = sci.split_once('e').expect("{:e} has an exponent");
    let exp: i32 = exp.parse().expect("integer exponent");
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(m) => ("-", m),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();

    if (-4..16).contains(&exp) {
        let point = exp + 1; // digits before the decimal point
        if point <= 0 {
            format!("{sign}0.{}{digits}", "0".repeat((-point) as usize))
        } else if (point as usize) >= digits.len() {
            format!("{sign}{digits}{}.0", "0".repeat(point as usize - digits.len()))
        } else {
            let (int, frac) = digits.split_at(point as usize);
            format!("{sign}{int}.{frac}")
        }
    } else {
        let (first, rest) = digits.split_at(1);
        let frac = if rest.is_empty() { String::new() } else { format!(".{rest}") };
        let esign = if exp < 0 { '-' } else { '+' };
        format!("{sign}{first}{frac}e{esign}{:02}", exp.abs())
    }
}

/// `str(value)` for a JSON-decoded value.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        Value::Number(n) => number_repr(n),
        other => py_repr(other),
    }
}

/// `repr(value)` for a JSON-decoded value: `['a', 1]`, `{'k': None}`.
pub fn py_repr(value: &Value) -> String {
    match value {
        Value::String(s) => repr_str(s),
        Value::Array(items) => format!("[{}]", items.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter().map(|(k, v)| format!("{}: {}", repr_str(k), py_repr(v))).collect::<Vec<_>>().join(", ")
        ),
        scalar => py_str(scalar),
    }
}

/// `repr(s)`: single-quoted unless only double quotes avoid an escape;
/// unprintable characters escaped as Python does.
pub fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if c.is_control() => {
                let n = c as u32;
                out.push_str(&match n {
                    0..=0xff => format!("\\x{n:02x}"),
                    0x100..=0xffff => format!("\\u{n:04x}"),
                    _ => format!("\\U{n:08x}"),
                });
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python truthiness.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `int(value)`: bools and numbers convert (floats truncate), as do strings
/// that spell an integer; anything else is None.
pub fn py_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(b) => Some(*b as i64),
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f.trunc() as i64)),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

/// `float(value)`: None for anything it would raise on.
pub fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(b) => Some(*b as u8 as f64),
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

/// `type(value).__name__` for a JSON-decoded value.
pub fn py_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // Expected strings are Python 3.13's repr / json.dumps output.
    #[test]
    fn dumps_unicode_matches_python() {
        let v = json!({"a": "é\u{0}\u{7f}\n😀", "b": [1, 2.5]});
        assert_eq!(dumps_unicode(&v), "{\"a\": \"é\\u0000\u{7f}\\n😀\", \"b\": [1, 2.5]}");
    }

    #[test]
    fn float_repr_matches_python() {
        let cases = [
            (1.0, "1.0"),
            (0.1, "0.1"),
            (1e-05, "1e-05"),
            (1.5e-07, "1.5e-07"),
            (1e16, "1e+16"),
            (1.2345678901234568e16, "1.2345678901234568e+16"),
            (123456789012345.6, "123456789012345.6"),
            (1e22, "1e+22"),
            (0.0001, "0.0001"),
            (0.00012, "0.00012"),
            (-2.5, "-2.5"),
            (2.71, "2.71"),
            (1e100, "1e+100"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (12345678.0, "12345678.0"),
            (0.30000000000000004, "0.30000000000000004"),
            (100.0, "100.0"),
            (1e15, "1000000000000000.0"),
            (9999999999999998.0, "9999999999999998.0"),
            (0.0, "0.0"),
            (-0.0, "-0.0"),
        ];
        for (x, want) in cases {
            assert_eq!(float_repr(x), want, "{x:e}");
        }
    }

    #[test]
    fn dumps_matches_python() {
        let v = json!({
            "a": "\u{e9}\u{2028}\u{1F600}\u{7f}\u{1}\"\\/\n\t",
            "b": [1, 2.0, null, true, false],
            "c": {},
        });
        let want = concat!(
            r#"{"a": ""#,
            "\\u00e9\\u2028\\ud83d\\ude00\\u007f\\u0001",
            r#"\"\\/\n\t", "b": [1, 2.0, null, true, false], "c": {}}"#,
        );
        assert_eq!(dumps(&v), want);
        // Key order is insertion order, as in a Python dict.
        assert_eq!(dumps(&json!({"z": 1, "a": 2})), r#"{"z": 1, "a": 2}"#);
    }

    #[test]
    fn python_scalars() {
        assert_eq!(py_str(&json!(true)), "True");
        assert_eq!(py_str(&json!(null)), "None");
        assert_eq!(py_str(&json!(1.0)), "1.0");
        assert!(!truthy(&json!("")) && !truthy(&json!(0)) && !truthy(&json!({})) && truthy(&json!("x")));
        assert_eq!(py_int(&json!(3.7)), Some(3));
        assert_eq!(py_int(&json!(" 12 ")), Some(12));
        assert_eq!(py_int(&json!("x")), None);
    }

    #[test]
    fn repr_of_a_string() {
        assert_eq!(repr_str("google_genai:x"), "'google_genai:x'");
        assert_eq!(repr_str("it's"), "\"it's\"");
        assert_eq!(repr_str("both ' and \""), "'both \\' and \"'");
        assert_eq!(repr_str("a\\b\n\u{1}\u{e9}"), "'a\\\\b\\n\\x01\u{e9}'");
        assert_eq!(py_str(&json!(["a", 1, 2.5, null, true, {"k": "it's"}])), "['a', 1, 2.5, None, True, {'k': \"it's\"}]");
    }
}
