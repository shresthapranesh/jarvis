//! Throughput per model call — a port of `core/perf.py`. A call splits in
//! two at its first streamed output:
//!
//! ```text
//! prefill  start → first output   (prompt processing)
//! decode   first output → end     (generation)
//! ```
//!
//! Tokens served from the prompt cache did no prefill work and are left out
//! of the prefill rate. Ollama's own server-side durations, when it sends
//! them, beat the wall clock. A rate that can't be measured honestly is
//! `None` — "unknown", never 0.
//!
//! "First output" is the first text, thinking or tool-call piece. Python
//! stamps LangChain's first `on_llm_new_token`, which is the same moment for
//! most streams; a metadata-only event (Responses' `response.created`) isn't
//! output, and stamping it would put prefill at ~0.

use std::time::Instant;

use serde_json::{Value, json};

use super::Delta;
use super::transcript::Message;

/// A rate over a shorter span than this is clock noise.
const MIN_SPAN_SECONDS: f64 = 0.005;
/// A decode span shorter than this can't be told from a buffered flush, so
/// no eval rate is given for it (see `_MIN_DECODE_SECONDS` for the numbers).
const MIN_DECODE_SECONDS: f64 = 0.25;
/// One interval is the least evidence of a cadence.
const MIN_CHUNKS_FOR_EVAL: u32 = 2;
/// The newest calls a snapshot lists.
const KEEP_CALLS: usize = 50;

/// What a server measured of its own call (Ollama's `prompt_eval_*` and
/// `eval_*`), sent only when it has both spans.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ServerTimings {
    pub prefill_tokens: i64,
    pub prefill_seconds: f64,
    pub output_tokens: i64,
    pub decode_seconds: f64,
}

/// The wall clock around one attempt at a call.
pub struct Clock {
    started: Instant,
    first: Option<Instant>,
    chunks: u32,
    server: Option<ServerTimings>,
}

impl Clock {
    pub fn start() -> Self {
        Clock { started: Instant::now(), first: None, chunks: 0, server: None }
    }

    pub fn saw(&mut self, d: &Delta) {
        match d {
            Delta::Text(_) | Delta::Thinking(_) | Delta::ToolCall => {
                self.first.get_or_insert_with(Instant::now);
                self.chunks += 1;
            }
            Delta::Timings(t) => self.server = Some(*t),
        }
    }
}

/// One call's throughput (`LlmCallPerf`).
#[derive(Clone, Debug, PartialEq)]
pub struct CallPerf {
    pub model: String,
    pub input_tokens: i64,
    pub output_tokens: i64,
    /// Input served from the prompt cache — billed, but no prefill work.
    pub cached_input_tokens: i64,
    /// What prefill actually processed.
    pub prefill_tokens: i64,
    pub ttft_ms: Option<f64>,
    pub total_ms: f64,
    pub decode_ms: Option<f64>,
    pub prefill_seconds: Option<f64>,
    pub decode_seconds: Option<f64>,
    /// Output pieces seen; 0 = nothing streamed.
    pub chunks: u32,
    /// `provider` (server-side, exact), `measured` (wall clock either side
    /// of the first output), `prefill_only` (decode too short to trust) or
    /// `unsplit` (nothing streamed, no boundary).
    pub source: &'static str,
}

impl CallPerf {
    /// The call that just ended, from its clock and the record it produced.
    pub fn measure(model: &str, reply: &Message, clock: &Clock) -> Self {
        let total = clock.started.elapsed().as_secs_f64();
        let usage = reply.usage.clone().unwrap_or_default();
        let input = usage.input.unwrap_or(0);
        let mut output = usage.output.unwrap_or(0);
        let cached = usage
            .input_details
            .as_ref()
            .and_then(|d| d.get("cache_read"))
            .and_then(Value::as_i64)
            .unwrap_or(0)
            // A provider that doesn't fold cache reads into input would
            // otherwise give a negative prefill.
            .min(input);
        let mut prefill_tokens = (input - cached).max(0);

        let ttft = clock.first.map(|f| (f - clock.started).as_secs_f64());
        let mut decode = ttft.map(|t| (total - t).max(0.0));
        let prefill;
        let source;
        if let Some(s) = clock.server {
            // The server's counts are what its durations measured.
            if s.prefill_tokens != 0 {
                prefill_tokens = s.prefill_tokens;
            }
            if s.output_tokens != 0 {
                output = s.output_tokens;
            }
            prefill = Some(s.prefill_seconds);
            decode = Some(s.decode_seconds);
            source = "provider";
        } else {
            prefill = ttft;
            if ttft.is_none() {
                source = "unsplit";
            } else if clock.chunks < MIN_CHUNKS_FOR_EVAL || decode.unwrap_or(0.0) < MIN_DECODE_SECONDS {
                // A flush folded into the first-output time understates the
                // prefill rate — wrong in the safe direction, so it stays.
                decode = None;
                source = "prefill_only";
            } else {
                source = "measured";
            }
        }
        CallPerf {
            model: model.to_string(),
            input_tokens: input,
            output_tokens: output,
            cached_input_tokens: cached,
            prefill_tokens,
            ttft_ms: ttft.map(|t| t * 1000.0),
            total_ms: total * 1000.0,
            decode_ms: decode.map(|d| d * 1000.0),
            prefill_seconds: prefill,
            decode_seconds: decode,
            chunks: clock.chunks,
            source,
        }
    }

    pub fn prefill_tps(&self) -> Option<f64> {
        rate(self.prefill_tokens, self.prefill_seconds)
    }

    pub fn eval_tps(&self) -> Option<f64> {
        rate(self.output_tokens, self.decode_seconds)
    }

    pub fn to_json(&self) -> Value {
        json!({
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "prefill_tokens": self.prefill_tokens,
            "ttft_ms": self.ttft_ms.map(round1),
            "total_ms": round1(self.total_ms),
            "decode_ms": self.decode_ms.map(round1),
            "prefill_tps": self.prefill_tps().map(round1),
            "eval_tps": self.eval_tps().map(round1),
            "chunks": self.chunks,
            "source": self.source,
        })
    }
}

/// One decimal, for display. Python's `round` goes half-to-even on the exact
/// binary value; on a timing the difference is noise.
fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn rate(tokens: i64, seconds: Option<f64>) -> Option<f64> {
    match seconds {
        Some(s) if tokens != 0 && s >= MIN_SPAN_SECONDS => Some(tokens as f64 / s),
        _ => None,
    }
}

/// A run's calls (`PerfTracker`). Rates are token-weighted — total tokens
/// over total seconds — not a mean of per-call rates: a 50-token call and a
/// 5000-token call are not equal evidence.
#[derive(Default)]
pub struct PerfTracker {
    calls: Vec<CallPerf>,
    prefill_tokens: i64,
    prefill_seconds: f64,
    output_tokens: i64,
    decode_seconds: f64,
    llm_seconds: f64,
    /// The first call's — what the user waited for.
    first_ttft_ms: Option<f64>,
    llm_calls: i64,
}

impl PerfTracker {
    /// Add a call; returns the `perf_update` event's data.
    pub fn record(&mut self, call: CallPerf) -> Value {
        self.llm_calls += 1;
        self.llm_seconds += call.total_ms / 1000.0;
        if self.first_ttft_ms.is_none() {
            self.first_ttft_ms = call.ttft_ms;
        }
        // A fully cache-served prefill did no work; counting its time against
        // zero tokens would drag the rate down.
        if let Some(s) = call.prefill_seconds.filter(|&s| call.prefill_tokens > 0 && s != 0.0) {
            self.prefill_tokens += call.prefill_tokens;
            self.prefill_seconds += s;
        }
        if let Some(s) = call.decode_seconds.filter(|&s| call.output_tokens > 0 && s != 0.0) {
            self.output_tokens += call.output_tokens;
            self.decode_seconds += s;
        }
        tracing::debug!(
            "perf: {} ttft={:?}ms prefill={:?} tok/s ({} tok) eval={:?} tok/s ({} tok) [{}]",
            call.model,
            call.ttft_ms.map(round1),
            call.prefill_tps().map(round1),
            call.prefill_tokens,
            call.eval_tps().map(round1),
            call.output_tokens,
            call.source
        );
        self.calls.push(call);
        if self.calls.len() > KEEP_CALLS {
            self.calls.drain(..self.calls.len() - KEEP_CALLS);
        }
        let mut data = self.message_perf().expect("a call was recorded");
        data["llm_calls"] = self.llm_calls.into();
        data["snapshot"] = self.snapshot();
        data
    }

    fn prefill_tps(&self) -> Option<f64> {
        rate(self.prefill_tokens, Some(self.prefill_seconds))
    }

    fn eval_tps(&self) -> Option<f64> {
        rate(self.output_tokens, Some(self.decode_seconds))
    }

    pub fn snapshot(&self) -> Value {
        json!({
            "ttft_ms": self.first_ttft_ms.map(round1),
            "llm_ms": round1(self.llm_seconds * 1000.0),
            "prefill_tps": self.prefill_tps().map(round1),
            "eval_tps": self.eval_tps().map(round1),
            "prefill_tokens": self.prefill_tokens,
            "output_tokens": self.output_tokens,
            "llm_calls": self.llm_calls,
            "calls": self.calls.iter().map(CallPerf::to_json).collect::<Vec<_>>(),
        })
    }

    /// The four values a chat turn stores on its Message row, or `None`
    /// before any call.
    pub fn message_perf(&self) -> Option<Value> {
        (self.llm_calls > 0).then(|| {
            json!({
                "ttft_ms": self.first_ttft_ms.map(round1),
                "llm_ms": round1(self.llm_seconds * 1000.0),
                "prefill_tps": self.prefill_tps().map(round1),
                "eval_tps": self.eval_tps().map(round1),
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn reply(input: i64, output: i64, cached: i64) -> Message {
        serde_json::from_value(json!({"v": 1, "role": "assistant", "content": "",
            "usage": {"input": input, "output": output, "input_details": {"cache_read": cached}}}))
        .unwrap()
    }

    /// A clock that started `ago` and saw its first output `first` in.
    fn clock(ago: f64, first: Option<f64>, chunks: u32) -> Clock {
        let started = Instant::now() - Duration::from_secs_f64(ago);
        Clock { started, first: first.map(|f| started + Duration::from_secs_f64(f)), chunks, server: None }
    }

    #[test]
    fn splits_at_the_first_output() {
        let p = CallPerf::measure("m", &reply(1000, 100, 400), &clock(2.0, Some(0.5), 20));
        assert_eq!((p.source, p.prefill_tokens, p.cached_input_tokens), ("measured", 600, 400));
        assert!((p.prefill_tps().unwrap() - 1200.0).abs() < 5.0);
        assert!((p.eval_tps().unwrap() - 100.0 / 1.5).abs() < 1.0);
    }

    #[test]
    fn withholds_what_it_cant_measure() {
        // A flush: the output arrived just before the end.
        let p = CallPerf::measure("m", &reply(1000, 7, 0), &clock(1.0, Some(0.99), 3));
        assert_eq!((p.source, p.eval_tps()), ("prefill_only", None));
        assert!(p.prefill_tps().is_some());
        let p = CallPerf::measure("m", &reply(1000, 7, 0), &clock(1.0, None, 0));
        assert_eq!((p.source, p.prefill_tps(), p.eval_tps()), ("unsplit", None, None));
        // Cache reads beyond the input don't make prefill negative.
        let p = CallPerf::measure("m", &reply(10, 1, 50), &clock(1.0, Some(0.5), 5));
        assert_eq!((p.cached_input_tokens, p.prefill_tokens), (10, 0));
    }

    #[test]
    fn server_timings_win() {
        let mut c = clock(3.0, Some(0.1), 1);
        c.saw(&Delta::Timings(ServerTimings {
            prefill_tokens: 50,
            prefill_seconds: 0.5,
            output_tokens: 0,
            decode_seconds: 2.0,
        }));
        let p = CallPerf::measure("m", &reply(60, 8, 0), &c);
        assert_eq!((p.source, p.prefill_tokens, p.output_tokens), ("provider", 50, 8));
        assert_eq!((p.prefill_tps(), p.eval_tps()), (Some(100.0), Some(4.0)));
    }

    #[test]
    fn tracker_weights_by_tokens() {
        let mut t = PerfTracker::default();
        assert_eq!(t.message_perf(), None);
        let call = |prefill: i64, ps: f64, out: i64, ds: f64| CallPerf {
            model: "m".into(),
            input_tokens: prefill,
            output_tokens: out,
            cached_input_tokens: 0,
            prefill_tokens: prefill,
            ttft_ms: Some(ps * 1000.0),
            total_ms: (ps + ds) * 1000.0,
            decode_ms: Some(ds * 1000.0),
            prefill_seconds: Some(ps),
            decode_seconds: Some(ds),
            chunks: 10,
            source: "measured",
        };
        t.record(call(100, 1.0, 50, 1.0));
        let data = t.record(call(900, 1.0, 50, 4.0));
        assert_eq!(data["prefill_tps"], json!(500.0));
        assert_eq!(data["eval_tps"], json!(20.0));
        assert_eq!(data["ttft_ms"], json!(1000.0));
        assert_eq!(data["llm_ms"], json!(7000.0));
        assert_eq!(data["llm_calls"], json!(2));
        assert_eq!(data["snapshot"]["calls"].as_array().unwrap().len(), 2);
    }
}
