# jarvis-edge

The Rust front of the jarvis server. Phase 1 of moving off Python: the edge owns
the public port, answers the GraphQL operations that have been ported, and
reverse-proxies everything else to the Python server behind it. Rust owns
the database reads, GraphQL, the job queue's triggers, the event stream and
every timer; Python is reduced to a worker that runs agent jobs, and with
`JARVIS_WORKER_CMD` set the edge starts it when there is work and stops it when
idle — so an idle box runs no Python at all (see "The worker").

```
browser / SDK ──▶ edge :8000 ──(ported operation)──▶ SQLite
                       │
                       └──(everything else)──▶ Python :8001
```

## Running it

```bash
# The edge on :8000 (what vite and the jarvis SDK already target), starting
# Python on :8001 when it's needed and stopping it after 5 idle minutes:
cd edge && JARVIS_APP_DIR=.. JARVIS_WORKER_CMD='exec .venv/bin/uvicorn server.entrypoint:app --port $JARVIS_BACKEND_PORT' cargo run

# …or run Python yourself (always on, with --reload), and the edge in front:
JARVIS_EDGE_URL=http://127.0.0.1:8000 uv run uvicorn server.entrypoint:app --reload --port 8001
cd edge && cargo run
```

Docker runs the first way via `edge/serve.sh`. Python on its own on :8000
still works, since the edge is a strict front and nothing in Python depends on
it.

| env | default | |
|---|---|---|
| `JARVIS_EDGE_BIND` | `127.0.0.1:8000` | edge listen address |
| `JARVIS_BACKEND_URL` | `http://127.0.0.1:8001` | the Python server |
| `JARVIS_EDGE_LOG` | `info` | `error`…`trace`, the edge's own logs only |
| `DATABASE_URL` / `WORK_DIR` | as `core/config.py` | same database file as Python |
| `ARTIFACTS_DIR` / `DOCUMENTS_DIR` / `STAGING_DIR` / `CHECKPOINTS_DB` | as `core/config.py` | same files as Python |
| `JARVIS_WORKER_CMD` | unset | the command that runs Python (via `sh -c`, in `JARVIS_APP_DIR`); set, the edge owns the worker |
| `JARVIS_WORKER_IDLE` | `300` | seconds idle before the worker is stopped; `0` keeps it up (restarted if it dies) |
| `JARVIS_APP_DIR` | the current directory | the jarvis checkout: where the worker runs, and `static/dist`, the SPA the edge serves |

The built-in model list is compiled in from `core/builtin_models.json`, so
rebuild the edge after editing it.

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

## The worker link (`/internal/worker`)

Runs still execute in Python, but the edge starts them and serves everyone
watching them. Python dials a loopback-only WebSocket on the edge
(`core/edge_link.py`, when `JARVIS_EDGE_URL` is set) and reports its run
registry: each run's registration, every event `emit_event` appends (raw
`{"event", "data"}` records), state changes (done, cancelled, interrupt, token
counters) and removal, plus `holds` — why it mustn't be stopped for being
idle (see "The worker"). The edge keeps a mirror (`src/runs.rs`) and steers
back over the same socket (protocol 4):

| edge → worker | |
|---|---|
| `cancel` | the in-process half of a stop |
| `wake` | a job was just committed; claim it now, not at the next poll |
| `adopt_queued` | re-read the conversation's queued messages (see below) |
| `call` → `reply` | run a function that needs the run's in-memory state — `queue_message`, `unqueue_message`, `resume_task`, `resume_workflow_run`, `resolve_workflow_approval` — and return its result or the error message Python's resolver would raise; and `drain` / `undrain`, which stop and restart job claims before an idle stop |

- **Nothing is durable on the link.** Every (re)connect starts with a
  snapshot of `_tasks` including each run's full event history, which
  reconciles everything: an edge restart, a dropped link, a worker restart.
  `hello.instance` says whether a reconnect is the same process (runs carry
  over, subscribers keep their place) or a new one (every mirrored run is
  gone; its subscribers end with the DB fallback).
- **Events are raw; typing is the edge's.** The same `done` record is a
  `DoneEvent` to `taskEvents` and an `AutomationDoneEvent` to
  `automationRunEvents`, so each subscription coerces (`src/gql/events.rs`),
  keeping Python's `data.get` / truthiness / `str()` semantics and
  `json.dumps` byte for byte for the fields that embed JSON text
  (`src/pyjson.rs`).
- **The registration race.** A run Python starts itself (a bot, the
  scheduler, the board dispatcher) is handed out over one channel and
  reported over another. A subscription for an unknown run whose DB row still
  says "in progress" waits up to 2 s for it to register.
- **Only while linked — or owned.** The subscription socket, `runningTasks`,
  the stop mutations and the triggers are served by the edge while a worker
  is linked, or always when the edge owns the worker (no worker up then means
  no run in flight, and a trigger's job starts one). Otherwise they go to
  Python as before.

### Runs the edge starts

`startTask`, `runWorkflow` and `triggerAutomation` (`src/gql/start.rs`) write
what Python's `register_*` functions wrote — the conversation, the user
message, attachments copied from staging into `documents_dir` as `Document`
rows, the domain row the run reports into, and the `jobs` row — in one
transaction. The run's model is resolved here too (`src/catalog.rs`), from the
same `core/builtin_models.json` Python loads plus the `models.custom` and
`default.model` settings rows.

Python's triggers registered a `TaskState` before committing, so a subscriber
could never miss the run. The edge does the same in its own mirror: the run is
**pending** — the edge's, not yet any worker's — until a worker claims the job
and registers it. Then:

- **The claim continues the run.** The worker creates the run's state from
  the job (`get_or_create_task_state(job=...)`): started at the job's
  `created_at`, so queue wait still counts, and already cancelled if a stop
  arrived first. Its event 0 is appended after anything the edge emitted
  while the run was pending (`worker_base`), so a subscriber's cursor carries
  across. The trigger's label stands.
- **A pending run is the edge's to answer for.** A message queued onto it is
  a `queued` row plus a `queued_message` event the edge emits; the worker's
  chat handler adopts queued rows at claim. If the claim and the queue cross,
  the edge sends `adopt_queued` (with the ids it couldn't announce itself), so
  the worker reads the conversation again. A pending run has no interrupt to
  answer.
- **A stop on a pending run is durable**: `cancel_requested` on the still-
  pending job, rather than Python's pending → `cancelled`, which left the
  `TaskState` and the message row in progress forever. The worker that claims
  it runs it already cancelled, so it finishes as stopped with its rows
  written.
- **A pending run survives a new worker process** — no worker had it — and
  leaves the mirror only when its job ends unclaimed (a 5 s sweep against
  `jobs`, for a handler that returned before registering, say).

## The scheduler (`src/schedule.rs`, `src/cron.rs`)

Every timer the Python server ran is the edge's, so that between jobs Python
has nothing to do:

| timer | when | does |
|---|---|---|
| each enabled automation | its cron schedule | enqueues an `automation` job |
| board dispatch | every 15 s, and when Python sends `dispatch` | `dispatch_board_tasks`, in the edge |
| memory consolidation | `0 */6 * * *` | enqueues a `maintenance` job, if due |
| project memory | every 30 min | enqueues a `maintenance` job, if due |
| checkpoint prune | `20 * * * *` | enqueues a `maintenance` job, if due |
| staging cleanup | `0 * * * *` | deletes abandoned uploads, in the edge |
| memory-activity prune | `0 4 * * *` | deletes old access-log rows, in the edge |

Python behind the edge (`JARVIS_EDGE_URL` set — `core/edge_link.py:behind_edge`)
registers none of these but the idle-kernel reaper (kernels are its own
children). Its `dispatch_board_tasks()` sends `dispatch` instead of claiming
cards itself, `_register_scheduler_job` / `_remove_scheduler_job` send
`schedules`, and a `maintenance` worker runs the three sweeps that need it
(`core/scheduler.py:MAINTENANCE_TASKS`). The decision is configuration, not
link state, so a reconnecting link can't leave both sides firing.

- **Cron is APScheduler's, not a library's.** `cron.rs` ports
  `CronTrigger.from_crontab` (after `normalize_crontab`) with Python's
  `zoneinfo` arithmetic: day-of-month AND day-of-week, a fire time in a
  spring-forward gap keeping its wall clock with the pre-transition offset,
  `fold` in an overlap. `tests/test_edge_schedule.py` diffs it against
  APScheduler on 13,200 cases — 14 zones, every 2026 DST transition, 50
  expression shapes — and `Automation.nextRunAt` against Python's.
- **Firing follows APScheduler's job options**: missed runs coalesce, a run
  later than its grace period (60 s for automations) is skipped, and nothing
  missed while the edge was down is caught up.
- **A schedule is re-checked at fire time** against the row, so one disabled
  or deleted a moment ago doesn't fire from a stale copy. Schedules reload
  300 ms after `schedules` and every 60 s regardless.
- **The zone** is the `scheduler.timezone` setting, else `JARVIS_TIMEZONE`,
  else `TZ`, else the system zone, else UTC — read at startup, as Python reads
  it.
- **Maintenance jobs coalesce**: none is enqueued while one for the same
  sweep is pending or running, so a machine that was off doesn't come back to
  a backlog.
- **…and wait for work** (`Scheduler::maintenance_due`), because a job starts
  Python. Each sweep's own first checks, read from the same rows: a message
  past the memory watermark whose first isn't a reply still being written; a
  project with new messages that has been quiet 15 minutes (or waited a day)
  and holds 600+ characters; a checkpoint the prune's age and keep-3 rules
  would delete. The watermarks are LangGraph store items in `checkpoints.db`
  (`src/checkpoints.rs`, read-only). Where unsure (an unreadable watermark, a
  failed read) the answer is yes, and Python decides as before.
  `tests/test_edge_supervisor.py` diffs every gate against the sweep it
  guards, through `jarvis-edge --maintenance-due`. One thing waits longer: the
  first-run seeding of discrete memory from the old blob now happens with the
  first pass that has a message to read.

## The worker (`src/supervisor.rs`)

With `JARVIS_WORKER_CMD` set the edge owns the Python process: it runs the
command (in its own process group, with `JARVIS_EDGE_URL` and
`JARVIS_BACKEND_PORT` set) when there is work, and stops it when there has
been none for `JARVIS_WORKER_IDLE` seconds. Idle, jarvis is the edge alone.

**What starts it**: a request the edge proxies (REST, the other WebSockets,
GraphQL it hasn't ported) — which waits for it, 2–3 s on a laptop; a job a
worker could claim now, or one a dead worker left `running`; a worker that
reported it must stay up dying (see holds); and the edge's own start, so the
startup sweeps run and a broken command shows up at once. A run the edge
starts itself needs nothing more: its job kicks the supervisor, and the run is
pending in the mirror until the new worker claims it.

**What keeps it up**: a proxied request or socket in progress (a log stream
holds it while someone watches), a run in the mirror, a claimable or running
job, and the worker's `holds` — `telegram` / `discord` (a bot is connected:
always on, restarted if it dies) and `kernels` (a conversation's notebook
still has its variables; held until the kernel's 30-minute idle reap, as
before). Ready means `/health` answers and the link has said hello.

**How it stops**: `call drain` — the worker stops claiming and waits out a
claim in flight — then one last look at the job table and the mirror. Work
that slipped in means `undrain`; otherwise SIGTERM to the group (uvicorn's
graceful shutdown: kernels, MCP servers, the link) and SIGKILL after 30 s. The
next worker is started with `JARVIS_EDGE_RESPAWN=1`, which tells its startup
that nothing crashed: it skips the incognito sweep, which would otherwise
delete an incognito chat open in a tab between turns. The zombie sweep needs
no flag — a row whose job is still pending was never claimed, and is skipped
(`cleanup_zombie_running_rows`).

**When it fails**: a command that won't start, isn't ready in 120 s, or dies
within a minute of starting backs off exponentially to a minute; requests meanwhile
get a 503 naming the reason.

**What a page load needs without it** is served here: the SPA from
`$JARVIS_APP_DIR/static/dist` (every GET that isn't one of Python's routes —
`proxy.rs:python_get_route`, checked against the app's route table by the
tests), `/health`, and the queries the chat page makes — `models`, `todos`
and `browserAvailable`. A resolver that meets data only Python reads
faithfully (a checkpoint in another encoding, an https CDP endpoint, a
`models.custom` row Python itself would fail on) returns an `edgeDefer` error,
and `graphql.rs` answers the whole operation in Python instead.

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
| automations | `automations` (with `nextRunAt`), `automation`, `automationRuns` |
| task board | `boardTasks`, `boardTask` |
| workflows | `workflows`, `workflow`, `workflowRuns`, `workflowRun` |
| lists | `notificationChannels`, `skills`, `pendingApprovals` |
| memory | `memories`, `memoryActivities`, `memoryUsage` |
| chat page | `models`, `todos` (from `checkpoints.db`), `browserAvailable` (an http CDP endpoint) |
| Relay | `node` for every Node type |

Mutations that only write rows and files:

| Domain | Root fields |
|---|---|
| conversations | `updateConversation` — title and pin only, see below |
| projects | `createProject`, `updateProject`, `deleteProject`, `setConversationProject` |
| artifacts & documents | `updateArtifact`, `restoreArtifactVersion`, `deleteArtifact`, `deleteDocument` |
| workflows | `createWorkflow`, `updateWorkflow`, `deleteWorkflow` (human callers) |
| lists | `createNotificationChannel`, `updateNotificationChannel`, `deleteNotificationChannel`, `deleteSkill` (human callers) |
| memory | `deleteMemory` |
| runs (worker linked or owned) | `stopRunningTask`, `stopTask`, `stopAutomationRun`, `stopWorkflowRun` |
| starting runs (worker linked or owned) | `startTask`, `runWorkflow`, `triggerAutomation` |
| steering runs (worker linked or owned) | `queueMessage`, `unqueueMessage`, `resumeTask`, `resumeWorkflowRun`, `resolveWorkflowApproval` — through `call` once a worker has the run |

And while a worker is linked, or the edge owns it: every subscription
(`taskEvents`, `automationRunEvents`, `boardTaskEvents`, `workflowRunEvents`)
and `runningTasks`, from the run mirror.

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
| `agentMemory`, `checkpointStats` | LangGraph's store, and the prune's live-thread guard | the agent loop (Phase 2) |
| `modelSync` | provider APIs | the catalog tooling |
| `tools` | the bound-tool list, the SDK catalogue, loaded MCP tools | the agent loop |
| `mcpServers`, `mcpTools` | the live `McpManager` | MCP (Phase 2) |
| `settings`, `setting` | the `KNOWN_SETTINGS` registry in `core/settings_admin.py` | the registry becoming data |
| `voiceStatus` | Piper voice file layout in `core/voice.py` | audio |

| Mutations | Touch | Move with |
|---|---|---|
| `stopBoardTask`, `browserActivity` | the board row and a running handler's `TaskState`; the agent's kernel is the only caller of the latter | the board dispatcher; the agent loop |
| `deleteConversation`, `discardConversation` | the LangGraph thread and the conversation's kernel | the agent loop |
| `createAutomation`, `updateAutomation`, `deleteAutomation` | cron validation with APScheduler's messages; deleting the backing conversation's LangGraph thread and kernel | the agent loop (Python reports each change to the edge's scheduler: `schedules`) |
| `createBoardTask`, `updateBoardTask`, `setBoardTaskStatus`, `answerBoardTask`, `decomposeBoardTask`, `deleteBoardTask`, `stopBoardTask` | the board dispatcher, model validation, an LLM (decompose) | the job queue |
| `addMemory`, `updateMemoryItem`, `createSkill`, `updateSkill` | embeddings (Gemini) on write | embeddings |
| `updateMemory`, `deleteAgentMemory`, `consolidateMemory`, `consolidateProjectMemory` | the LangGraph store; an LLM | the agent loop |
| `addModel`, `updateModel`, `addDiscoveredModels`, `removeModel`, `setDefaultModel`, `setToolPolicy` | the catalog cache and compiled agent graphs | the catalog becoming data |
| `addMcpServer`, `updateMcpServer`, `removeMcpServer`, `reloadMcpServers`, `setMcpServerLoadMode`, `setMcpDefaultLoadMode`, `callMcpTool` | the live `McpManager` | MCP |
| `resolveApproval`, `requestToolApproval` | waiters parked inside running tools | the job queue |
| `setSetting`, `deleteSetting` | `apply_setting`'s in-process caches | the registry becoming data |
| `pruneCheckpoints`, `downloadVoice` | `checkpoints.db` guarded by `_tasks`; the Piper download | the agent loop; audio |
