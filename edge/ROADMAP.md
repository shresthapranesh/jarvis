# Moving jarvis to Rust — what's left

The goal is everything in Rust: the Python server gone entirely. Only the
notebook kernel and the `jarvis` SDK inside it stay Python, because the
agent writes Python in `run_cell`. Until then, the edge starts Python on
demand (`JARVIS_WORKER_CMD`) for whatever isn't ported yet.

Sizes are relative: S ≈ a session, M ≈ a few, L ≈ many. Tick an item when
it's on `main`.

## Done

- [x] Edge in front: every read, row-only writes, subscriptions, run triggers, stops (PR #76)
- [x] Scheduler, board dispatch, on-demand Python worker (PR #76)
- [x] Telegram and Discord bots (PR #78)
- [x] Transcript out of LangGraph, into `database.db` (2a)
- [x] Model clients: Gemini, Ollama, OpenRouter, Meta, OpenAI-compatible endpoints (PR #80)
- [x] Notebook kernels run by the edge (PR #80)
- [x] Agent loop: chat turns, retrieval, embeddings, summarizing (PR #81)
- [x] Automations and board tasks run in the edge (PR #82)
- [x] Board writes, conversation deletes, model change (PR #83)

## A. Finish the agent loop — no turn hands over to Python

- [x] `write_artifact`: markdown and files, versions, the `artifact` event
- [x] Approvals: gated tools wait in the edge; `resolveApproval` (gates, board questions), `requestToolApproval`
- [x] `resolveApproval` for deferred deletes (`delete_workflow`, `delete_automation`, `delete_skill`) and every denial
- [x] `resolveApproval` for an approved `call_mcp_tool`
- [x] Attachments: removed instead of ported (uploads, documents and their index, image input, the bots' photos)
- [x] Workers: `spawn_workers`, its roles and their tools (files, artifact reads, document search)
- [x] Anthropic client: the Messages API, streamed, as `ChatAnthropic` sends it
- [x] Bedrock client: ConverseStream, signed by `aws.rs`, as `ChatBedrockConverse` sends it (credentials only boto3 reads stay Python's)
- [x] MCP: our own client (stdio, Streamable HTTP, HTTP+SSE, websocket), servers, `mcpServers`/`mcpTools`, the server mutations, `callMcpTool`, bound `always` tools, `mcp.*` settings; Python behind the edge calls through it

## B. The remaining API

- [x] Memory and skill writes: `addMemory`, `updateMemoryItem`, `createSkill`, `updateSkill`
- [x] `decomposeBoardTask` (an LLM call)
- [x] Automations: `createAutomation`, `updateAutomation`, `deleteAutomation` (a human's; an agent's delete is approval-gated in Python)
- [x] Agent memory: `agentMemory`, `updateMemory`, `deleteAgentMemory`
- [x] Memory consolidation and project memory (the maintenance sweeps, `consolidateMemory`, `consolidateProjectMemory`)
- [x] Settings: `settings`, `setting`, `setSetting`, `deleteSetting`, managed keys and `mcp.*` included
- [x] Models: `addModel`, `updateModel`, `removeModel`, `setDefaultModel`, `addDiscoveredModels`, endpoints
- [x] Tools: `tools`, `setToolPolicy`
- [x] `modelSync`: provider model listings (Gemini, Anthropic, Bedrock, Ollama, OpenRouter, endpoints) and the probe calls (an AWS credential source only boto3 reads goes to Python)
- [x] `browserActivity` (an edge run's stream; a worker's run is Python's)
- [x] REST: `/artifacts/{id}/raw`, `/server-logs` (+ `/stream`, the edge's and a linked worker's records)

## C. The big pieces

- [x] Workflow engine: `workflow/engine.py` and the node types, workflow runs, `run_workflow`
- [x] Voice: removed instead of ported (Whisper, Piper, `downloadVoice`, `voiceStatus`, `/ws/live`, the Live page)
- [x] `/ws/browser`: the live view, the browser launched when none is up
- [x] CLI: `jarvis-edge run|config|model|memory|start` (`main.py`'s `download-voice` went with voice, `maintenance` with 2a step 11)

## D. Switch Python off

- [x] Schema and migrations owned by the edge (from `db/engine.py:_migrate`) — M
- [x] Parity tests diff against Python's answers, recorded (`tests/python_golden.py`)
- [x] 2a step 11: drop `langgraph-checkpoint-sqlite`, `checkpoints.db` conversion, edge `checkpoints.rs`
- [x] Delete `server/`, the worker link, the supervisor and the proxy; the server-only Python dependencies
- [ ] Delete Python's former runtime (`core/agents.py`, the loop, `workflow/`, `main.py`) and langchain — with the kernel decision below

### What still reaches Python (audit, 2026-10-06)

Every path by which the edge hands work to Python today. Each has to be
ported, turned into an edge error, or found to be unreachable once Python is
gone, before `server/` can be deleted.

**Turns handed over mid-run** (`src/agent/`)

- [x] Tool arguments that aren't exactly what the schema asks (`tools::native`
  → `Plan::Python`): Python words Pydantic's error to the model. The workers
  already word these themselves (`workers.rs`); do the same for the main
  agent's tools — M
- [x] Prompt context the edge can't build: `system_prompt.md` unreadable or a
  context read failing fails the turn (`prompt::Unbuilt`); an https CDP
  endpoint is probed in Rust — S
- [x] A job the edge can't start: an unreadable thread, a chat job without its
  payload, an automation of an input type it doesn't know, an error preparing
  a board or workflow run — fails the run (`Agent::fail_start`, `queue::fail`) — S

**GraphQL the edge defers** (`gql::defer`, 58 sites; `graphql.rs`)

- [x] Errors Python words: invalid JSON in settings, MCP and approval payloads,
  a malformed `models.custom` row, values `agentMemory` would coerce, a provider
  listing `modelSync` can't read, a context window GraphQL can't carry — word
  them in Rust — M
- [x] State only a Python run holds: an answer to a request no live run waits
  on is expired here, with Python's "no longer waiting". What's left — the
  `run.claimed()` checks in `approval.rs`, `mcp.rs`, `browser.rs` — is reached
  only through a linked worker's run, so it goes with the worker link — S
- [x] A model the edge doesn't call (consolidation, project memory, the board
  planner, `run` in the CLI, runs): every run is routed to the edge and a
  model it can't call fails with the reason; embedding failures in memory
  writes fail the write — S
- [ ] AWS credential sources only boto3 reads (assume-role, SSO, web identity,
  `credential_process`, a container role) — for `modelSync` and Bedrock turns:
  port the ones worth having, declare the rest unsupported — decision, M–L
- [x] Requests the router won't take: batched arrays, multipart or non-JSON
  bodies, a field or argument that fails validation, a deferring field beside
  others — Strawberry's 400s in its words, executing's errors, a refusal for
  the deferring field — S

**Startup work only Python does** (`server/entrypoint.py` lifespan)

- [x] The incognito sweep: conversations a crash left behind
  (`sweep_ephemeral_conversations`), at the edge's start with the agent loop
  on (`agent/sweep.rs`). Python behind the edge still sweeps too, until it goes
  (with `JARVIS_EDGE_RESPAWN`) — S
- [x] After an edge crash, the run rows no live job stands behind and the
  approvals whose waiter died (`cleanup_zombie_running_rows`,
  `reconcile_startup`), at the edge's start (`agent/sweep.rs`). Not ported:
  `_backfill_board`, a one-time backfill for tasks blocked before the
  approvals table existed — S–M

**The command line** (`Fail::Python` → exec `main.py`)

- [x] A `models.custom` row Python rejects, an `AGENTS.md` row or file that
  isn't text, a `model sync` deferral, `run` on a model the edge doesn't call —
  errors in Rust. Only `run` with `JARVIS_AGENT_RUNTIME=python` still execs
  `main.py` — S

**The kernel** (stays Python by design) — what it still imports

- [ ] The `jarvis` SDK loads `core.config`, `core.embeddings` (and with it
  `langchain-google-genai` / `langchain-ollama`, for `search_memory`),
  `core.retrieval`, `core.tool_gate` and `core.tool_policy` (with `db/` and
  SQLAlchemy, for the in-kernel gate), `core.text_dedupe`, and
  `tools.research` / `tools.browser`. Decide: keep a slim `core/` + `db/` for the
  kernel, or route embeddings and gates through the edge so the kernel needs
  only `httpx` and the browser — decision, M

## Not yet tried for real

- [ ] Bots with real Telegram and Discord tokens
- [ ] The Docker image built and run (`CMD ["jarvis-edge"]`)
