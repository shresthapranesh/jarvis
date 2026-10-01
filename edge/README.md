# jarvis-edge

The Rust front of the jarvis server. Phase 1 of moving off Python: the edge owns
the public port, answers the GraphQL operations that have been ported, and
reverse-proxies everything else to the Python server behind it. The end state
is Rust owning the database, GraphQL, the job queue and the event stream, with
Python reduced to a worker that runs agent jobs. Then an idle box runs no
Python at all.

```
browser / SDK ──▶ edge :8000 ──(ported operation)──▶ SQLite
                       │
                       └──(everything else)──▶ Python :8001
```

## Running it

```bash
# terminal 1 — Python, moved off :8000
uv run uvicorn server.entrypoint:app --reload --port 8001
# terminal 2 — the edge, on :8000 (what vite and the jarvis SDK already target)
cd edge && cargo run
```

Docker runs both via `edge/serve.sh`. Python on its own on :8000 still works,
since the edge is a strict front and nothing in Python depends on it.

| env | default | |
|---|---|---|
| `JARVIS_EDGE_BIND` | `127.0.0.1:8000` | edge listen address |
| `JARVIS_BACKEND_URL` | `http://127.0.0.1:8001` | the Python server |
| `JARVIS_EDGE_LOG` | `info` | `error`…`trace`, the edge's own logs only |
| `DATABASE_URL` / `WORK_DIR` | as `core/config.py` | same database file as Python |

## Routing

An operation is answered by the edge only when **every root field it selects**
is defined in the edge's schema. The schema reads its root fields back from its
own SDL, so the routing table can't drift from the code. Everything else goes
to Python:

- subscriptions and the mutations not yet ported,
- introspection,
- `node(id:)` when the id names a type the edge can't resolve,
- any operation that fails the edge's validation, e.g. it selects a field on an
  owned type that isn't ported. Logged at warn, because it means a type was
  only partly ported.

Splitting one operation across both servers is never attempted.

## Contracts with the Python side

- **Python owns the schema** (`init_db` + `_migrate`). The edge creates no
  tables and opens the same file with the same pragmas, plus
  `foreign_keys = OFF`, which sqlx would otherwise turn on.
- **Wire formats match byte for byte** (`src/gql/codec.rs`):
  - global ids are `base64("Type:id")`
  - `DateTime` is Python's `isoformat()`, which drops a zero fraction
  - message cursors are urlsafe `base64("{iso}|{id}")`
- **`/server-logs` peer check.** Python's localhost-only check sees every
  proxied request as 127.0.0.1, so the edge enforces it on the real peer.

## Porting a domain

1. Port the type and its query resolvers under `src/gql/`, and add the query
   object to `Query` in `src/gql/mod.rs`. Port **every** field of a type: a
   partly ported type makes owned operations fail validation and fall back on
   every call.
2. If the type is a Relay `Node`, add it to `Node` and `NODE_TYPES` in
   `src/gql/node.rs`.
3. Add the frontend operations that are now owned to `PARITY_OPERATIONS` in
   `tests/test_edge_parity.py`, with a test that seeds rows and diffs Python
   against the edge. `test_every_claimed_frontend_query_is_diffed` fails until
   you do.
4. `uv run pytest tests/test_edge_parity.py` and `cargo test` in `edge/`.

## What the edge serves

Every query that reads only the database and files:

| Domain | Root fields |
|---|---|
| conversations | `conversations`, `conversation` (+ the message connection) |
| projects | `projects`, `project` |
| artifacts & documents | `artifacts`, `artifact`, `artifactVersions`, `documents` |
| automations | `automationRuns` |
| task board | `boardTasks`, `boardTask` |
| workflows | `workflows`, `workflow`, `workflowRuns`, `workflowRun` |
| lists | `notificationChannels`, `skills`, `pendingApprovals` |
| memory | `memories`, `memoryActivities`, `memoryUsage` |
| Relay | `node` for every Node type except `Automation` |

Mutations that only write rows and files:

| Domain | Root fields |
|---|---|
| conversations | `updateConversation` — title and pin only, see below |
| projects | `createProject`, `updateProject`, `deleteProject`, `setConversationProject` |
| artifacts & documents | `updateArtifact`, `restoreArtifactVersion`, `deleteArtifact`, `deleteDocument` |
| workflows | `createWorkflow`, `updateWorkflow`, `deleteWorkflow` (human callers) |
| lists | `createNotificationChannel`, `updateNotificationChannel`, `deleteNotificationChannel`, `deleteSkill` (human callers) |
| memory | `deleteMemory` |

Three are owned per call (`router.rs:Walk::field_rule`):
`updateConversation` goes to Python when it sets `model` (validated against the
model catalog), and `deleteWorkflow` / `deleteSkill` go to Python when the
caller is the agent (`X-Jarvis-Caller: agent` — approval-gated there).

A write must leave a row exactly as SQLAlchemy would: `uuid4()` ids, its
stored timestamp text, `updated_at` bumped by hand where `onupdate=_now`
would have, and ORM cascades spelled out as explicit DELETEs — including
the ones that *don't* happen (a memory's access log outlives it, because
foreign keys are off). The mutation tests run each mutation through Python
on one database and the edge on a copy, then diff every table and file.

## What stays in Python, and why

These answer from state the Python process holds, not from rows. Each one
moves when the thing it reads moves.

| Root field | Reads | Moves with |
|---|---|---|
| `automations`, `automation` | `nextRunAt` is APScheduler's next fire time, DST handling included | the scheduler |
| `runningTasks` | the in-memory `_tasks` registry | the job queue + event stream |
| `todos`, `agentMemory`, `checkpointStats` | LangGraph's checkpointer and store (`checkpoints.db`, serialized) | the agent loop (Phase 2) |
| `models`, `modelSync` | the built-in catalog compiled into `core/model_catalog.py`; provider APIs | the catalog becoming data |
| `tools` | the bound-tool list, the SDK catalogue, loaded MCP tools | the agent loop |
| `mcpServers`, `mcpTools` | the live `McpManager` | MCP (Phase 2) |
| `settings`, `setting` | the `KNOWN_SETTINGS` registry in `core/settings_admin.py` | the registry becoming data |
| `voiceStatus` | Piper voice file layout in `core/voice.py` | audio |
| `browserAvailable` | a CDP probe that may be `https://` (the edge has no TLS yet) | the edge gaining TLS |

| Mutations | Touch | Move with |
|---|---|---|
| `startTask`, `stopTask`, `queueMessage`, `unqueueMessage`, `resumeTask`, `stopRunningTask`, `runWorkflow`, `stopWorkflowRun`, `resumeWorkflowRun`, `resolveWorkflowApproval`, `triggerAutomation`, `stopAutomationRun`, `browserActivity` | `TaskState`, the job queue, run handlers | the job queue + event stream |
| `deleteConversation`, `discardConversation` | the LangGraph thread and the conversation's kernel | the agent loop |
| `createAutomation`, `updateAutomation`, `deleteAutomation` | scheduler registration | the scheduler |
| `createBoardTask`, `updateBoardTask`, `setBoardTaskStatus`, `answerBoardTask`, `decomposeBoardTask`, `deleteBoardTask`, `stopBoardTask` | the board dispatcher, model validation, an LLM (decompose) | the job queue |
| `addMemory`, `updateMemoryItem`, `createSkill`, `updateSkill` | embeddings (Gemini) on write | embeddings |
| `updateMemory`, `deleteAgentMemory`, `consolidateMemory`, `consolidateProjectMemory` | the LangGraph store; an LLM | the agent loop |
| `addModel`, `updateModel`, `addDiscoveredModels`, `removeModel`, `setDefaultModel`, `setToolPolicy` | the catalog cache and compiled agent graphs | the catalog becoming data |
| `addMcpServer`, `updateMcpServer`, `removeMcpServer`, `reloadMcpServers`, `setMcpServerLoadMode`, `setMcpDefaultLoadMode`, `callMcpTool` | the live `McpManager` | MCP |
| `resolveApproval`, `requestToolApproval` | waiters parked inside running tools | the job queue |
| `setSetting`, `deleteSetting` | `apply_setting`'s in-process caches | the registry becoming data |
| `pruneCheckpoints`, `downloadVoice` | `checkpoints.db` guarded by `_tasks`; the Piper download | the agent loop; audio |
