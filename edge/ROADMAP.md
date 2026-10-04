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
- [ ] `resolveApproval` for an approved `call_mcp_tool` — with MCP
- [ ] Attachments: text extraction at turn start (PDF, docx, xlsx, …) — M–L
- [ ] Workers: `spawn_workers` — M
- [ ] Anthropic client — M
- [ ] Bedrock client — M
- [ ] MCP via `rmcp`: servers, `mcpServers`/`mcpTools`, `callMcpTool`, `jarvis.mcp_call` — L

## B. The remaining API

- [x] Memory and skill writes: `addMemory`, `updateMemoryItem`, `createSkill`, `updateSkill`
- [x] `decomposeBoardTask` (an LLM call)
- [x] Automations: `createAutomation`, `updateAutomation`, `deleteAutomation` (a human's; an agent's delete is approval-gated in Python)
- [x] Agent memory: `agentMemory`, `updateMemory`, `deleteAgentMemory`
- [x] Memory consolidation and project memory (the maintenance sweeps, `consolidateMemory`, `consolidateProjectMemory`)
- [ ] Settings: `settings`, `setting`, `setSetting`, `deleteSetting` (the `KNOWN_SETTINGS` registry) — M
- [ ] Models: `addModel`, `updateModel`, `removeModel`, `setDefaultModel`, `addDiscoveredModels`, `setToolPolicy`, endpoints, `modelSync` — M
- [ ] `tools` query, `browserActivity` — S
- [ ] REST: file upload/download, log tailing — M

## C. The big pieces

- [ ] Workflow engine: `workflow/engine.py` and the node types, workflow runs — L
- [ ] Voice: Whisper transcription, Piper TTS, `downloadVoice`, `voiceStatus`, `/ws/live` — L (Whisper alone is ~245 MB in Python)
- [ ] `/ws/browser` — M
- [ ] CLI: `main.py` → a Rust binary — M

## D. Switch Python off

- [ ] Schema and migrations owned by the edge (from `db/engine.py:_migrate`) — M
- [ ] Parity tests that diff against Python become Rust-only tests — M
- [ ] 2a step 11: drop `langgraph-checkpoint-sqlite`, `checkpoints.db` conversion, edge `checkpoints.rs`
- [ ] Delete `server/`, the worker link and supervisor, and the Python dependencies (langchain and the rest)

## Not yet tried for real

- [ ] Bots with real Telegram and Discord tokens
- [ ] The Docker image (`edge/serve.sh`) built and run
