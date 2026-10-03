//! A run's ceilings — a port of `core/budget.py`. Counts tokens, model calls
//! and tool calls, and says when a run went past a limit; the caller stops
//! the run. Each change hands back the events `emit_event` would append:
//! `budget_update` (throttled — every event stays in the run's replay
//! buffer) and `budget_exceeded` once.

use std::time::Instant;

use serde_json::{Map, Value, json};

/// An event name and its data.
pub type Event = (&'static str, Value);

/// Emit `budget_update` only when the totals moved this much.
const EMIT_MIN_TOKEN_DELTA: i64 = 1000;
const EMIT_MIN_CALL_DELTA: i64 = 10;

/// Ceilings for one run; `None` is unlimited (`BudgetLimits`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Limits {
    pub max_total_tokens: Option<i64>,
    pub max_input_tokens: Option<i64>,
    pub max_output_tokens: Option<i64>,
    pub max_llm_calls: Option<i64>,
    pub max_tool_calls: Option<i64>,
    pub max_duration_seconds: Option<i64>,
    /// Carried, not enforced — as in Python.
    pub max_messages: Option<i64>,
}

impl Limits {
    /// The defaults for a run kind (`KIND_BUDGET_DEFAULTS`; an unknown kind
    /// gets chat's), each overridden by its `JARVIS_BUDGET_MAX_*` variable.
    pub fn for_kind(kind: &str) -> Self {
        Self::for_kind_with(kind, |k| std::env::var(k).ok())
    }

    fn for_kind_with(kind: &str, var: impl Fn(&str) -> Option<String>) -> Self {
        let (tokens, llm, tools) = match kind {
            "automation" => (600_000, 200, 300),
            "workflow" | "board_task" => (400_000, 150, 200),
            _ => (500_000, 200, 300),
        };
        let env = |name: &str| -> Option<i64> {
            let raw = var(name).filter(|r| !r.is_empty())?;
            match raw.trim().parse() {
                Ok(n) => Some(n),
                Err(_) => {
                    tracing::warn!("Invalid int for {name}={raw:?} — ignoring");
                    None
                }
            }
        };
        // `_int_env(A) or _int_env(B)`: a 0 falls through to the old name.
        let total = env("JARVIS_BUDGET_MAX_TOTAL_TOKENS").filter(|&n| n != 0).or_else(|| env("JARVIS_BUDGET_MAX_TOKENS"));
        Limits {
            max_total_tokens: total.or(Some(tokens)),
            max_input_tokens: env("JARVIS_BUDGET_MAX_INPUT_TOKENS"),
            max_output_tokens: env("JARVIS_BUDGET_MAX_OUTPUT_TOKENS"),
            max_llm_calls: env("JARVIS_BUDGET_MAX_LLM_CALLS").or(Some(llm)),
            max_tool_calls: env("JARVIS_BUDGET_MAX_TOOL_CALLS").or(Some(tools)),
            max_duration_seconds: env("JARVIS_BUDGET_MAX_DURATION_SECONDS").or(Some(1800)),
            max_messages: env("JARVIS_BUDGET_MAX_MESSAGES"),
        }
    }

    /// The set limits, in field order (`to_dict`).
    fn to_json(&self) -> Value {
        let fields = [
            ("max_total_tokens", self.max_total_tokens),
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("max_llm_calls", self.max_llm_calls),
            ("max_tool_calls", self.max_tool_calls),
            ("max_duration_seconds", self.max_duration_seconds),
            ("max_messages", self.max_messages),
        ];
        Value::Object(fields.into_iter().filter_map(|(k, v)| Some((k.to_string(), v?.into()))).collect::<Map<_, _>>())
    }
}

/// One run's counts against its [`Limits`] (`BudgetTracker`).
pub struct Budget {
    pub limits: Limits,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub llm_calls: i64,
    pub tool_calls: i64,
    started: Instant,
    exceeded: Option<String>,
    last_emitted_tokens: i64,
    last_emitted_calls: i64,
}

impl Budget {
    pub fn new(limits: Limits) -> Self {
        Budget {
            limits,
            input_tokens: 0,
            output_tokens: 0,
            llm_calls: 0,
            tool_calls: 0,
            started: Instant::now(),
            exceeded: None,
            last_emitted_tokens: -1,
            last_emitted_calls: -1,
        }
    }

    pub fn total_tokens(&self) -> i64 {
        self.input_tokens + self.output_tokens
    }

    /// Why the run went over, once it has.
    #[allow(dead_code, reason = "the Rust agent loop (phase 2d) is its caller")]
    pub fn exceeded(&self) -> Option<&str> {
        self.exceeded.as_deref()
    }

    /// A model call, with the usage it reported. A call that reported none
    /// still counts toward `max_llm_calls`.
    pub fn record_llm(&mut self, input: Option<i64>, output: Option<i64>) -> Vec<Event> {
        self.input_tokens += input.unwrap_or(0);
        self.output_tokens += output.unwrap_or(0);
        self.llm_calls += 1;
        self.changed()
    }

    #[allow(dead_code, reason = "the Rust agent loop (phase 2d) is its caller")]
    pub fn record_tool(&mut self, count: i64) -> Vec<Event> {
        self.tool_calls += count;
        self.changed()
    }

    /// Look again with no new counts — the duration limit runs on its own.
    pub fn check(&mut self) -> Vec<Event> {
        self.check_exceeded().into_iter().collect()
    }

    /// Python syncs (and maybe emits the update) before it checks, so the
    /// update for the call that went over doesn't say so yet.
    fn changed(&mut self) -> Vec<Event> {
        let mut events: Vec<Event> = self.update().into_iter().collect();
        events.extend(self.check_exceeded());
        events
    }

    fn update(&mut self) -> Option<Event> {
        let calls = self.llm_calls + self.tool_calls;
        let due = self.last_emitted_tokens < 0
            || self.total_tokens() - self.last_emitted_tokens >= EMIT_MIN_TOKEN_DELTA
            || calls - self.last_emitted_calls >= EMIT_MIN_CALL_DELTA
            || self.exceeded.is_some();
        if !due {
            return None;
        }
        self.last_emitted_tokens = self.total_tokens();
        self.last_emitted_calls = calls;
        Some((
            "budget_update",
            json!({
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens(),
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "snapshot": self.snapshot(),
            }),
        ))
    }

    fn check_exceeded(&mut self) -> Option<Event> {
        if self.exceeded.is_some() {
            return None;
        }
        let elapsed = self.started.elapsed().as_secs_f64();
        let l = &self.limits;
        let over = |n: i64, max: Option<i64>| max.filter(|&m| n > m);
        let reason = if let Some(m) = over(self.total_tokens(), l.max_total_tokens) {
            format!("total tokens {} > limit {m}", self.total_tokens())
        } else if let Some(m) = over(self.input_tokens, l.max_input_tokens) {
            format!("input tokens {} > limit {m}", self.input_tokens)
        } else if let Some(m) = over(self.output_tokens, l.max_output_tokens) {
            format!("output tokens {} > limit {m}", self.output_tokens)
        } else if let Some(m) = over(self.llm_calls, l.max_llm_calls) {
            format!("llm calls {} > limit {m}", self.llm_calls)
        } else if let Some(m) = over(self.tool_calls, l.max_tool_calls) {
            format!("tool calls {} > limit {m}", self.tool_calls)
        } else if let Some(m) = l.max_duration_seconds.filter(|&m| elapsed > m as f64) {
            format!("duration {elapsed:.1}s > limit {m}s")
        } else {
            return None;
        };
        tracing::warn!("budget exceeded: {reason}");
        self.exceeded = Some(reason.clone());
        Some(("budget_exceeded", json!({"reason": reason, "snapshot": self.snapshot()})))
    }

    pub fn snapshot(&self) -> Value {
        json!({
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens(),
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": self.started.elapsed().as_secs(),
            "limits": self.limits.to_json(),
            "exceeded_reason": self.exceeded,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(events: &[Event]) -> Vec<&str> {
        events.iter().map(|(n, _)| *n).collect()
    }

    #[test]
    fn limits_per_kind_and_env() {
        let none = |_: &str| None;
        let chat = Limits::for_kind_with("chat", none);
        assert_eq!(chat.to_json(), json!({"max_total_tokens": 500000, "max_llm_calls": 200,
                                          "max_tool_calls": 300, "max_duration_seconds": 1800}));
        assert_eq!(Limits::for_kind_with("nonsense", none), chat);
        assert_eq!(Limits::for_kind_with("workflow", none).max_llm_calls, Some(150));
        let env = |k: &str| match k {
            "JARVIS_BUDGET_MAX_TOTAL_TOKENS" => Some("0".into()),
            "JARVIS_BUDGET_MAX_TOKENS" => Some(" 42 ".into()),
            "JARVIS_BUDGET_MAX_OUTPUT_TOKENS" => Some("7".into()),
            "JARVIS_BUDGET_MAX_LLM_CALLS" => Some("lots".into()),
            _ => None,
        };
        let l = Limits::for_kind_with("chat", env);
        assert_eq!((l.max_total_tokens, l.max_output_tokens, l.max_llm_calls), (Some(42), Some(7), Some(200)));
    }

    #[test]
    fn updates_are_throttled() {
        let mut b = Budget::new(Limits::default());
        assert_eq!(names(&b.record_llm(Some(100), Some(10))), ["budget_update"]);
        assert!(b.record_llm(Some(500), Some(10)).is_empty());
        assert!(b.record_tool(1).is_empty());
        // 1000 tokens since the last update (110 → 1110).
        assert_eq!(names(&b.record_llm(Some(490), None)), ["budget_update"]);
        for _ in 0..9 {
            assert!(b.record_tool(1).is_empty());
        }
        let events = b.record_tool(1);
        assert_eq!(events[0].1["tool_calls"], json!(11));
        assert_eq!(b.llm_calls, 3);
        // No usage still counts as a call.
        b.record_llm(None, None);
        assert_eq!((b.llm_calls, b.total_tokens()), (4, 1110));
    }

    #[test]
    fn going_over_is_said_once() {
        let mut b = Budget::new(Limits { max_llm_calls: Some(1), ..Default::default() });
        b.record_llm(Some(1), Some(1));
        let events = b.record_llm(Some(1), Some(1));
        assert_eq!(names(&events), ["budget_exceeded"]);
        assert_eq!(events[0].1["reason"], json!("llm calls 2 > limit 1"));
        assert_eq!(events[0].1["snapshot"]["exceeded_reason"], json!("llm calls 2 > limit 1"));
        assert_eq!(b.exceeded(), Some("llm calls 2 > limit 1"));
        // Once over, every change updates, and the reason isn't repeated.
        assert_eq!(names(&b.record_tool(1)), ["budget_update"]);
        assert!(b.check().is_empty());
    }

    #[test]
    fn duration_is_checked_without_new_counts() {
        let mut b = Budget::new(Limits { max_duration_seconds: Some(0), ..Default::default() });
        std::thread::sleep(std::time::Duration::from_millis(5));
        let events = b.check();
        assert_eq!(names(&events), ["budget_exceeded"]);
        assert!(events[0].1["reason"].as_str().unwrap().starts_with("duration 0.0s > limit 0s"));
    }
}
