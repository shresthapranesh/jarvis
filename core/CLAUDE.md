# core/ — agent runtime notes

## Agent loop (`agent_loop.py`, `agents.py`, `messages.py`, `compaction.py`)
- `agent_loop.Agent` is the loop (no LangGraph): model step → tool batch → repeat. `build_agent(model, board=False)` builds the main agent (and its worker roles) once per model id + flags; anything that changes what's bound (tool policy, MCP load mode, catalog edits) must call `invalidate_agent_cache()`.
- History is a `Thread`: `DbThread` (transcript tables, keyed by `configurable.thread_id`; a LangGraph-only thread is converted on first load, or earlier by the `convert_checkpoints` sweep) or an in-memory `Thread()` for one-shot runs (workers, workflow nodes, CLI, `/ws/live`) — pass `thread=`.
- Writes are per message and ordered: the model's reply (with its tool calls) before any tool runs, each tool result once it and the calls before it finish. A re-claimed chat job continues from the rows; its prompt id is derived from the task id (`chat_runtime.user_message_id`) so it replaces itself.
- `recursion_limit` counts steps (model calls + tool batches), LangGraph's meaning: 100 ≈ 50 model calls, then `RecursionLimitReached` (chat finishes `done`).
- `astream(..., subgraphs=True)` yields LangGraph's `(ns, mode, data)` chunks (`ns` always `()`), so `streaming._process_chunk` reads them. After each step the loop waits until the reader has handled everything so far (`Run.step_done`) — events written straight onto `TaskState` (approval requests) must not overtake the step. Close the stream with `aclosing` when breaking out early; closing cancels the run.
- Tools reach the run through `tools/context.current_ctx()` (a contextvar, `agent_loop.current_run()`). A tool that raises fails the run (ToolNode's behaviour); bad arguments and unknown tools come back as error results.
- A nested run (a worker inside `spawn_workers`) merges the enclosing config, so the parent's budget/perf callbacks count its model calls. The token handler is attached to the model call only — never to tools — so nothing leaks between runs.
- The main model step (`model_request_node`) is the only chokepoint every main-agent LLM call goes through. Mid-run queued user messages are drained there (`_drain_queued_input`), never in the tool batch — a HumanMessage between a tool call and its result is an orphan pairing Anthropic/Bedrock reject.
- Project context is re-read every iteration and must never be captured inside `_build_agent` (agents are shared across conversations).
- **The edge runs chat turns too** (`edge/src/agent/`, by default; `JARVIS_AGENT_RUNTIME=python` turns it off): `prompt.rs` ports this module's context assembly (system prompt, segments, todos/planning tail), `retrieve.rs`/`embed.rs` the retrieval and memory write below, `tools.rs` the todo tools and `remember`, `turn.rs` `_run_agent_task` and this loop. A change to any of them is made in both; `tests/test_edge_loop.py` diffs the two. A bound tool's schema change needs `edge/src/agent/tools.json` re-exported (see that test).
- `tests/test_agent_golden.py` replays scripted runs (`tests/agent_harness.py`) and compares events, steps, model requests and the thread with `tests/golden/agent/`. Re-record (`JARVIS_UPDATE_GOLDEN=1`) only for an intended change, and read the diff.

### Prompt layout and caching (`context_cache.py`, `build_llm_messages`)
A prefix cache is invalidated from the first changed byte, so **each call's payload must start with the previous call's payload**. With `cache=True`, requests are laid out most-stable-first:
```
system:  static prompt ▸bp  cacheable segments + conversation summary ▸bp
history: normalized; rolling ▸bp on the newest markable message
tail:    one user message, <turn_context>…</turn_context> — everything volatile
```
- Volatile content (retrieved memories, todos, project memory, planning directive, ranked skill shortlist) goes in the tail, never the system message.
- Volatile-part producers return `list[CacheSegment]` tagged with `name` + `cacheable`; `model_request_node` sorts by `_SEGMENT_STABILITY`. New segment → new rank entry.
- Anything in the cached system region must be stable *across* turns too (don't drop segments on trivial turns).
- Breakpoint spelling is per provider: `cache_control` for Anthropic/OpenRouter, a standalone `{"cachePoint": …}` block for Bedrock (which drops `cache_control` silently). Bedrock caching is Claude-only. `honors_cache_control()` in `model_catalog.py` is the single decision point.
- Cache TTL is 5m unless `JARVIS_CACHE_TTL=1h` (anthropic only; at 5m the `ttl` key is omitted).
- Per-call compaction moves its boundary in steps of 4 so 3 calls in 4 keep their cached prefix.
- `tests/test_prompt_cache_layout.py` asserts the prefix property through real integrations.
- `edge/src/llm/shape.rs` ports `strip_historical_thinking`, `repair_orphan_tool_calls`, `build_llm_messages` and this layout for the Rust agent loop — change both; `tests/test_edge_llm.py` diffs them.

### Compaction
- `maybe_compact()` → `CompactionResult` (`.messages` already leaned, `.state_update`, `.compacted`, `.episode`, `.evicted_ids`).
- The token check uses the provider's `usage_metadata.input_tokens` minus an estimate of the non-history overhead (`usage_overhead_tokens`) — **not** a tokenizer, which is a blocking network call for Google/Anthropic. Ollama opts out (`None`). Fallback counting runs in `asyncio.to_thread`.
- Threshold: `compact_threshold(model)` = 40% of `ModelSpec.context_window`, clamped [12k, 200k]; flat 80k when unknown; `JARVIS_COMPACT_TOKEN_THRESHOLD` overrides.
- `summarization.py` is deprecated — use `compaction.maybe_compact`.
- `edge/src/agent/summarize.rs` + `edge/src/llm/compact.rs` port `maybe_compact` and `core/episodes.py:record_episode` for the Rust loop — change both; `tests/test_edge_loop.py` diffs them.

### Transcript format (`transcript.py`, `transcript_format.md`)
- The v1 record of a message — what the agent loop stores per message (in place of LangGraph checkpoints), and what the Rust loop will read. Versioned and lossless: a LangChain message encodes and decodes back equal; anything not mapped to a field rides in `extras`.
- A change to the format is a new version, made in `transcript_format.md` first. `main.py maintenance check-transcript` round-trips a real `checkpoints.db`.

## Model catalog (`model_catalog.py`, `builtin_models.json`, `model_discovery.py`)
- Catalog = `BUILTIN_MODELS` (from `builtin_models.json`; first entry is the compile-time `DEFAULT_MODEL`) ∪ custom models in the `models.custom` setting. Ids are `provider:model_name`; providers: ollama, google_genai, bedrock, anthropic, meta, openrouter, or an endpoint's name. A new model from those needs no code — `main.py model add` or Settings → Models.
- The edge compiles `builtin_models.json` in too (`edge/src/catalog.rs`) — rebuild it after editing.
- **Endpoints** (`models.endpoints`, Settings → Models → Endpoints): named OpenAI-compatible servers (OpenAI, Groq, vLLM, LM Studio…). The name is a provider (`groq:llama-3.3-70b`), built as `ChatOpenAI` at its `base_url`; Sync lists its `GET /models`. Validate providers with `known_providers()`, not `KNOWN_PROVIDERS` (built-ins only), after `db.ops.hydrate_catalog` — the endpoint cache is per-process like the custom-model one. The API key is write-only: never return it (the `settings` query redacts it via `settings_admin.redact`). `edge/src/catalog.rs:parse_endpoints` must skip exactly what `parse_endpoints` skips.
- `context_window`: leave `None` unless known (too large disables compaction until the provider errors). Always `None` for `ollama:*` — the server's `num_ctx`, not the model card, truncates.
- OpenRouter: `ChatOpenAI` at `OPENROUTER_BASE_URL`; `stream_usage=True` is mandatory (otherwise budget/perf see zero tokens). Ids split on the first colon, so `:free` suffixes survive.
- **Stale model ids**: rows (`Automation.model`, `Conversation.model`, `BoardTask.model`, workflow node config, `default.model`, queued Job payloads) can name a removed model. Read paths degrade: `db.ops.resolve_model(explicit, session)` → default → seed; `resolve_model_spec(id)` is the sync last resort at `_build_agent`/`build_llm()`. Don't add validity checks after `resolve_model`.
- `model sync` (and the `modelSync` query) is a lint, not a source of truth. Discovery is blocking IO → `asyncio.to_thread`. A skipped provider means no data, not "in sync". Listing ≠ entitlement — only `--probe` checks access. In Relay, alias every `id` in sync results to `modelId` or it overwrites `ModelSpec` records.

## Memory (`memory_store.py`, `memory_consolidation.py`, `episodes.py`, `retrieval.py`)
- **With an embedder**: discrete `Memory` rows (`core` always injected; `fact` retrieved per turn into the tail). Agent writes via the bound `remember`; searches via `jarvis.search_memory`.
- **Without**: one `AGENTS.md` blob in `kv_store`.
- Consolidation (every 6h / `consolidateMemory`): watermark = last message consumed; batches of ≤16KB, ≤6 per pass, oldest first; stops before any `status="running"` row; 30% delete cap per pass.
- **Episodes**: each compaction chunk's summary is stored as a `ConversationEpisode` and retrieved into the tail on later turns. A `prefetch_retrieval` call that omits `conversation_id` disables episodes for that turn.
- **Hybrid retrieval**: dense cosine + BM25 via FTS5, fused with RRF (never a weighted sum of raw scores). Never pass user text to `MATCH` — use `fts_match_expr()`. Zero results is valid; callers must handle an empty list. Thresholds are per-install env vars (`JARVIS_MEMORY_MIN_COSINE`, etc.).

## MCP (`mcp.py`)
- Config merge order env (`JARVIS_MCP_SERVERS`) < file (`~/.jarvis/mcp.json`, …) < DB (`mcp.servers`). Tools are loaded per server (`get_tools(server_name=...)`) — the only source of attribution.
- Load modes per server: `always` (bound) or `lazy` (reached via `jarvis.mcp_call`, advertised by name in the `mcp_servers` segment). Stored as `"x-jarvis-load"` in the connection dict plus a separate `mcp.load_modes` override map. `strip_jarvis_keys()` must remove it before the client sees it (otherwise `TypeError` on connect).
- `call_tool` invokes with a ToolCall payload so `ToolMessage.status` distinguishes MCP errors from success.
- Tests: `tests/test_mcp_integration.py` spawns real stdio servers — keep it that way.

## Approvals and tool gating (`approval.py` = answer parsing, `approvals.py`, `tool_gate.py`, `tool_gate_node.py`, `tool_policy.py`)
- Every approval is a durable `Approval` row. **Blocking** (`action IS NULL`): a run is suspended now. **Deferred** (`action` set): the operation was recorded instead of performed; approving executes it (`ACTIONS`).
- `resolveApproval` → `approvals.resolve` is the single entry point. `is_affirmative_answer` denies on anything ambiguous. Deferred actions execute before the row closes.
- Only workflow approval/human_input nodes pause a run on `TaskState` (`set_interrupt()` / `clear_interrupt()` + `resume_future`; never set `pending_interrupt_id` alone). A chat run never pauses that way — it blocks inside the gated call.
- Rows are closed at chokepoints: `db.ops.update_board_task`, `streaming._finalize_message`, the resume mutations, `answer_board_task` (closes as `answered` *before* updating the task).
- `reconcile_startup()` runs after the zombie sweep: deferred and board rows stay; everything else (workflow, tool gate) expires.
- Deferred gating of agent writes (`gate_action`) is off unless `approval.required_actions` is set. Only `caller == "agent"` (the `X-Jarvis-Caller` header) is gated. Gate before any side effect.
- **Tool policy** (`tools.policy` setting, non-default entries only): disabled tools are unbound (filtered in `_build_agent` and hidden from `jarvis.help()`). Approval-required tools gate in the loop (`tool_gate_node.make_tool_gate`, run before each tool batch) — never by wrapping tools. All tool calls stay in history; a denied one gets its denial as its result.
- The tool gate uses the Approval row as the rendezvous (event + DB poll in-process, polling from the kernel). `run_cell`'s 60s timeout is suspended while a gate is open (`kernels.py:_hold_for_approval`).

## Budget and throughput (`budget.py`, `perf.py`)
- Each runtime creates a `BudgetTracker` + `BudgetCallbackHandler` per run; limits from `JARVIS_BUDGET_MAX_*` / `RunnerConfig`.
- `PerfTracker` splits each LLM call into prefill (to first chunk) and decode. Aggregates are token-weighted. Cache reads are subtracted from prefill. A decode span under 0.25s is `prefill_only` (buffered streams). Any rate may be `None` — render "unknown", never 0.

## Other modules
- `runner.py` — `JarvisRunner` owns store/queue/config; `should_use_cache()` true only for anthropic/bedrock.
- `planning.py` — `JARVIS_PLANNING_MODE` (auto/always/off); injects a `## Planning Required` tail segment for complex queries.
- `kernels.py` — per-conversation IPython kernels (cap 12, reaped at 30 min idle); injects SDK scope (`conversation_id`, `project_id`). Behind the edge `get_kernel_registry()` is `EdgeKernels`: the kernels are the edge's (`edge/src/kernels/`, a port — change both).
- `scheduler.py` — cron is local time (`scheduler.timezone` → `JARVIS_TIMEZONE` → machine zone), Unix day-of-week numbering via `normalize_crontab()`. Timezone is set before `_scheduler.start()`.
- `settings_admin.py` — `KNOWN_SETTINGS` registry + `apply_setting()` for in-process side effects of config writes. Keys with `managedBy` are owned by another settings tab.
