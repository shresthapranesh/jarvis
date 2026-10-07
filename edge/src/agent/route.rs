//! Whether the edge's agent loop takes the runs queued now: every chat turn,
//! automation run, board task and workflow run, unless
//! `JARVIS_AGENT_RUNTIME=python` leaves them all to Python. A model the edge
//! can't call — Bedrock with credentials only boto3 reads, a provider nobody
//! configured — fails the run with the reason. A turn that later needs
//! something only Python has is handed over mid-run (`queue::release`).

/// `JARVIS_AGENT_RUNTIME`: the edge runs the agent unless this says
/// `python`, which leaves every run to Python.
pub fn enabled() -> bool {
    !std::env::var("JARVIS_AGENT_RUNTIME").is_ok_and(|v| v.trim().eq_ignore_ascii_case("python"))
}
