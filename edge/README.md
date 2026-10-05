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
| `JARVIS_AGENT_RUNTIME` | `edge` | `python` leaves every chat turn to Python; otherwise the edge runs the ones it can (see "The agent loop") |

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
| `call` → `reply` | run a function that needs the run's in-memory state — `queue_message`, `unqueue_message`, `resume_workflow_run`, `resolve_workflow_approval` — and return its result or the error message Python's resolver would raise; and `drain` / `undrain`, which stop and restart job claims before an idle stop |

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
- **The registration race.** A run Python starts itself is handed out over
  one channel and reported over another. A subscription for an unknown run whose DB row still
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
| memory consolidation | `0 */6 * * *` | the sweep in the edge (`src/consolidate/`), if due |
| project memory | every 30 min | the sweep in the edge (`src/consolidate/`), if due |
| staging cleanup | `0 * * * *` | deletes abandoned uploads, in the edge |
| memory-activity prune | `0 4 * * *` | deletes old access-log rows, in the edge |

Python behind the edge (`JARVIS_EDGE_URL` set — `core/edge_link.py:behind_edge`)
registers none of these, nor the idle-kernel reaper: the kernels are the
edge's too (see "The kernels"). Its `dispatch_board_tasks()` sends `dispatch` instead of claiming
cards itself, `_register_scheduler_job` / `_remove_scheduler_job` send
`schedules`. The memory sweeps run in the edge when it calls the default
model (`consolidate::served`: the agent loop on, a provider it speaks);
otherwise they are queued as `maintenance` jobs for Python's worker
(`core/scheduler.py:MAINTENANCE_TASKS`), as before. The decision is configuration, not
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
  and holds 600+ characters. The watermarks are `kv_store` rows in
  `database.db`. Where unsure (an unreadable watermark, a
  failed read) the answer is yes, and Python decides as before.
  `tests/test_edge_supervisor.py` diffs every gate against the sweep it
  guards, through `jarvis-edge --maintenance-due`. One thing waits longer: the
  first-run seeding of discrete memory from the old blob now happens with the
  first pass that has a message to read.

## The bots (`src/bots/`)

The Telegram and Discord bots (`server/telegram_bot.py`,
`server/discord_bot.py`) run here, so an idle box with a bot connected runs
no Python. Each starts when its token is set (`TELEGRAM_BOT_TOKEN`,
`DISCORD_BOT_TOKEN`); Python behind the edge starts neither, by
configuration, as with the timers.

- **A message is a `startTask`.** It goes through the same `start_chat`
  (`src/gql/start.rs`), with the bot's surface and its conversation id
  (`telegram_<chat>`, `discord_<channel>`): a run already going on that chat
  takes it as a queued message (and the bot says so), otherwise a new run is
  mirrored as pending and its job wakes the worker. The rows are diffed
  against Python's own handlers (`tests/test_edge_bots.py`).
- **The reply follows the mirror**: `token` events from the main agent,
  edited into the chat at most once a second, created on the first text —
  never a placeholder — and finished when the run is done (or leaves the
  mirror).
- **Telegram** is the Bot API, long-polled; pending updates are dropped at
  start. `TELEGRAM_PROXY_URL` (or `HTTPS_PROXY` / `ALL_PROXY`) proxies it.
- **Discord** is the v10 gateway (heartbeat, zombie detection, resume, and a
  stop on a close no retry fixes, such as 4014 — Message Content Intent off)
  plus REST, retried on 429. Replies never ping anyone.
- **A voice note** is transcribed by Python's `/transcribe`, which starts the
  worker; Telegram shows "⏳ Transcribing…" meanwhile, Discord its typing
  indicator.
- **Notifications** (automation and workflow results) still go out from
  Python, now as plain Bot API / REST calls with the same tokens — no
  connected bot needed.
- `TELEGRAM_API_URL`, `DISCORD_API_URL` and `DISCORD_GATEWAY_URL` point the
  bots at another server; the tests' fake uses them.

## The worker (`src/supervisor.rs`)

With `JARVIS_WORKER_CMD` set the edge owns the Python process: it runs the
command (in its own process group, with `JARVIS_EDGE_URL` and
`JARVIS_BACKEND_PORT` set) when there is work, and stops it when there has
been none for `JARVIS_WORKER_IDLE` seconds. Idle, jarvis is the edge alone.

**What starts it**: a request the edge proxies (REST, the other WebSockets,
GraphQL it hasn't ported) — which waits for it, 2–3 s on a laptop; a job a
worker could claim now, or one a dead worker left `running` — not one the
edge's own agent loop runs (`jobs.runtime`); a voice note a
bot needs transcribed (Whisper is Python's); and the edge's own start, so the
startup sweeps run and a broken command shows up at once. A run the edge
starts itself needs nothing more: its job kicks the supervisor, and the run is
pending in the mirror until the new worker claims it.

**What keeps it up**: a proxied request or socket in progress (a log stream
holds it while someone watches), a run in the mirror that isn't the edge's
own, a claimable or running job (again, not the edge's), and the worker's `holds` (none today: a conversation's notebook is the
edge's, so it no longer keeps Python up — see "The kernels").
A connected chat bot holds nothing: the bots are the edge's (see "The bots"). Ready means `/health` answers and the link has said hello.

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

## The kernels (`src/kernels/`)

Phase 2c. The agent's notebooks — one `ipykernel` per session key (a
conversation, or a worker's own key), started on its first cell — are the
edge's children, not Python's. Python behind the edge runs `run_cell` here, so
an idle worker is stopped while a notebook keeps its variables, and the next
turn's worker finds them. A port of `core/kernels.py`; **a change to either is
made in both.**

- **The wire** (`wire.rs`, `kernel.rs`): the Jupyter messaging protocol over
  ZeroMQ — the pure-Rust `zeromq` crate, so no libzmq to build — on `ipc`
  sockets, HMAC-SHA256 signed. A kernel is `python -m ipykernel_launcher -f
  <connection file>` in its own process group, with `JPY_PARENT_PID` so it
  exits if the edge is killed. Ready means a `kernel_info_request` answered on
  shell *and* seen on iopub (a late subscriber misses what came before).
- **The interpreter** is `JARVIS_KERNEL_PYTHON`, else `$JARVIS_APP_DIR/.venv/bin/python`,
  else `python3`; the kernel runs in the checkout with it on `sys.path`, and
  `JARVIS_API_URL` pointed at this edge unless already set.
- **As `core/kernels.py` does it**: the `search`/`read` + `jarvis` preload, the
  SDK scoped per conversation and project, output assembled the same way
  (streams, results, a note for rich output, ANSI-free tracebacks, 30,000
  characters), a 60 s timeout that interrupts (SIGINT to the group) and keeps
  the session, held while a tool approval for the conversation is open (the
  `approvals` row, up to 30 minutes), 12 live kernels at most (least recently
  used goes), and one idle 30 minutes reaped (checked every 10).
- **`POST /internal/kernels/run`** `{key, code, timeout?, conversation_id?,
  project_id?}` → `{output}` or a 500 `{error}`; **`/shutdown`** `{key}`.
  Loopback only, `application/json` only, and refused with an `Origin` — a web
  page can't make a browser send that cross-site without a preflight, which
  nothing answers. Python's side is `core/kernels.py:EdgeKernels`, what
  `get_kernel_registry()` returns when `JARVIS_EDGE_URL` is set.
- **A caller that goes away** (a cancelled run closes its request) drops the
  handler, which interrupts the cell; the session's next cell first waits for
  that interrupt to land. A request the kernel receives before it has raised
  is aborted (`stop_on_error`) and would read as no output at all — Python's
  cancel path has that race.
- **Departures**, named in `tests/test_edge_kernels.py`: stdin is never
  offered, so `input()` raises at once (Python's client offered it with nobody
  to answer, and the cell hung until its timeout).

## The agent loop (`src/agent/`)

Phase 2d: chat turns, automation runs and board tasks run in the edge, so
none of them needs Python at all. On by
default (`JARVIS_AGENT_RUNTIME=python` turns it off).

- **The turn** (`turn.rs`) is `_run_agent_task` and `core/agent_loop.py`:
  the prompt into the thread and the plan reset, then model step, tool
  batch, repeat. Each message is written as it arrives, so a handover or a
  re-claim goes on from the rows (`thread.rs`, the transcript tables). Stops,
  the budget, the step limit, messages queued mid-run, and the leftover
  queue starting the next turn all behave as Python's.
- **The prompt** (`prompt.rs`) is `model_request_node`'s: the system prompt
  (read from the checkout), memory how-to, core and relevant memories, the
  skill catalog (ranked against the request past 8), earlier episodes, the
  live browser, the project, then the todo list or the planning directive.
  Retrieval runs once per user message, as Python's cache does. **A change to
  `core/agents.py`'s prompt is made in both.**
- **Retrieval** (`retrieve.rs`, `embed.rs`) ports `core/retrieval.py`
  (FTS5 + cosine, rank fusion, `select_hybrid`'s cutoffs), `search_memory`
  (with its access log), `search_skills`, `search_episodes` and
  `upsert_memory`. The embedder is the one Python picks — Gemini's
  `batchEmbedContents` with `GOOGLE_API_KEY`, else Ollama's `/api/embed`,
  model from `embedding.model` — with Python's query cache. **A change to
  either is made in both.**
- **Summarizing** (`summarize.rs`, the pure half in `llm/compact.rs`) ports
  `maybe_compact` and `record_episode`: past the model's threshold, the older
  groups are summarized by the turn's own model (merged into the running
  summary when there is one), the summary replaces them in the thread with
  the step's reply, and the evicted stretch's own summary is stored as an
  episode. History is counted from the last call's usage less the rest of the
  request, except on Ollama; otherwise by chars/4, which is what Python's
  fallback comes to for every provider here but Google (its countTokens API —
  a named departure). **A change to `core/compaction.py` or
  `core/episodes.py` is made in both.**
- **Automations** (`automation.rs`) port `automation_job_handler` around the
  same turn: the `AutomationRun` row (created at claim for a scheduled run),
  the thread (`automation_{id}` for a stateful run or a monitor, else
  `automation_{run_id}`), a stateful run's conversation messages, the
  monitor's wrapper and its `NO_CHANGE` gate, the skip for an overlapping
  stateful run, and the end — run status, `automationRunEvents`, and
  notifications (`src/notify.rs`, Telegram and Discord as Python sends them).
  Steps are announced but not written as rows, no plan reset, no throughput,
  the automation budget. The prompt's id is derived from the run, so a
  re-claimed run replaces it (Python gives it a fresh one). Code runs are
  the script on jarvis's interpreter (`JARVIS_KERNEL_PYTHON`, else the
  checkout's `.venv`) with output streamed by line, 60 s then killed, a stop
  terminating it; webhook runs are one request, 30 s, no redirects. **A
  change to `server/automation_runtime.py` or `core/notifications.py` is
  made in both.**
- **Board tasks** (`board.rs`) port `board_task_job_handler` and
  `tools/board.py` around the same turn, with `complete_task`/`block_task`
  bound (`tools::bound_for(…, board)`): the claim re-asserted and a waiting
  answer consumed (only once the edge is sure to run it), the task's prompt
  — skill, finished parents' handoffs — or the answer's resume prompt, the
  `boardtask_{id}` conversation, the tools' writes (a `needs_input` block
  asks in the inbox; any other move closes its question), and
  `_finish_task`, which never overwrites a task the run no longer owns. A
  task that finishes done starts a dispatch pass (which otherwise runs every
  15 s; `JARVIS_BOARD_DISPATCH_EVERY` shortens it, as the tests do). Steps are announced, not
  written; the board budget applies. **A change to
  `server/task_board_runtime.py` or `tools/board.py` is made in both.**
- **Stops through the job**: a running job's `cancel_requested` is polled
  every 5 s (`watch_queue_cancel`), so a stop that only reached the job —
  Python's `stopBoardTask`, served when no worker is linked — still stops the
  edge's run. The edge's own `stopBoardTask` stops it at once.
- **Tools** (`tools.rs`): the schemas are Python's own, exported to
  `tools.json` (re-export with `JARVIS_UPDATE_GOLDEN=1 uv run pytest
  tests/test_edge_loop.py -k schemas`). The edge runs `run_cell` (its own
  kernels), the todo tools, `remember` and `write_artifact`; an unknown tool
  gets ToolNode's error.
- **Artifacts** (`artifacts.rs`) port `write_artifact`: a markdown body or a
  file the agent wrote (a relative path from the checkout, as Python's
  working directory), the live file plus one copy per version, the rows, and
  the `artifact` event; an artifact from before versioning gets its file
  saved as v1 first. A file's type is `mimetypes.guess_type`'s
  (`src/mimetypes.rs`: Python's built-in table in `mimetypes.json`, then the
  system's `mime.types` files Python reads). **A change to
  `tools/artifacts.py` is made in both.**
- **Events and steps** (`events.rs`): tokens batched as `TokenCoalescer`
  does, each step's row written before its event.

- **Routing** (`route.rs`), when a turn or automation run is queued
  (`startTask`, `triggerAutomation`, a schedule firing): unless
  `JARVIS_AGENT_RUNTIME=python`, a turn on a provider the LLM layer speaks
  (Google, Ollama, OpenRouter, Meta, an OpenAI-compatible endpoint), with no
  attachments, and no MCP server configured anywhere Python looks (env, the
  first `mcp.json`, the `mcp.servers` setting) is the edge's — for an
  automation, a code or webhook one, or a prompt or monitor one on such a
  model; for a board task (at dispatch), one on such a model: its job gets
  `runtime = 'edge'` and its run is mirrored as the edge's own. Everything
  else is Python's, as before.
- **Claiming** (`queue.rs`) is `SqliteJobQueue._claim` plus `runtime =
  'edge'`, under the same thread lease, so a conversation's turns still run
  one at a time whichever side runs each. The lock is renewed at a third of
  its 300 s TTL; a lost lock abandons the turn.
- **Python leaves edge jobs alone**: its claim and lock reaper skip them, its
  startup sweep doesn't take their rows (or their open tool approvals) for a
  crashed run's, and the supervisor doesn't start or keep Python for them.
- **Handing over**: the job goes back to pending with `runtime` cleared and
  the run pending in the mirror, so a worker's claim continues it — events
  after the edge's, as for any pending run. A turn the edge had started
  carries `payload.handoff = {text, step_seq, steps, usage}`: Python then
  runs the tool calls the edge recorded but didn't run, and goes on from
  there (`chat_job_handler`), its text, step rows and spend continuing the
  edge's. A turn goes over when its next step needs what only Python has:
  a tool other than the edge's (workers, a workflow),
  arguments that aren't plainly valid, or a conversation not yet converted
  from `checkpoints.db`.
- **Approvals** (`src/approvals.rs`, a port of `core/tool_gate.py` and
  `core/approval.py` — change both): a call whose policy needs a human's yes
  records an `approvals` row, is shown in the chat (`approval_request`), and
  waits on the row, polled every 1.5 s, for `JARVIS_TOOL_GATE_TIMEOUT` (30
  minutes) before it expires as denied. Every gate in a batch is answered, in
  order, before anything runs; a denied call is answered with the denial.
  The answer comes through `resolveApproval` (`src/gql/approval.rs`), which
  the edge serves for a gate, a board task's question, and a deferred action
  (`core/approvals.py:ACTIONS` — a denial closes the row; an approved delete
  of a workflow, automation or skill runs here before the row closes, so a
  failure leaves it answerable). An approved MCP call, a workflow paused on
  a future, or a gate a worker's run is waiting on is deferred to Python
  before anything is written — which is
  why it's owned only alone in an operation. `requestToolApproval`, the
  SDK's request from a kernel, is the edge's on the same terms.
  Throughput measured before a handover isn't carried.
- **Recovery**: at start the edge re-queues its jobs a previous edge left
  running; with the loop off, it hands every live edge job to Python. Python
  running without the edge adopts them (`db/ops.py:adopt_edge_jobs`).

Tested in `tests/test_edge_agent.py` (routing, the queue, the handover's
Python side) and `tests/test_edge_loop.py`, which runs scripted turns
through both runtimes against one fake Ollama and diffs the events a
subscriber gets, the step rows, the message, the thread and every model
request. Departures are named there.

## The LLM layer (`src/llm/`)

Phase 2b: model calls from Rust, which the agent loop (`src/agent/`) makes.
`--llm-shape` and `--llm-call` drive it alone, reading one request as JSON
on stdin.

- `transcript.rs` — the v1 record (`core/transcript_format.md`). A row Python
  wrote reads and writes back equal, `null`s included.
- `shape.rs` — a port of `core/messages.py` (`strip_historical_thinking`,
  `repair_orphan_tool_calls`, `build_llm_messages`) and the cache layout in
  `core/context_cache.py`. **A change to either is made in both.** The
  result is a `Prompt` with breakpoints as flags; each provider spells them.
- One module per wire format, hand-written. No client library sits above
  our request builder, so the request is ours byte for byte. Each renders a
  `Prompt`, streams the reply (text and thinking deltas) and builds the
  assistant record:
  - `google.rs`: Gemini's `streamGenerateContent`.
  - `ollama.rs`: Ollama's `/api/chat`. Tool schemas as the `ollama`
    client's `Tool` model keeps them (an optional argument is `{}`).
  - `openai_chat.rs`: Chat Completions, for `openrouter`. It writes tool-call
    arguments the way Python's `json.dumps(ensure_ascii=False)` does, so a
    cached prefix stays the same bytes when a thread moves between runtimes.
  - `openai_responses.rs`: the Responses API, for `meta`, which is what
    `ChatMetaModel` uses. An assistant's text goes back with its server item
    id and `phase`.
- `complete()` retries a transient failure (429, 5xx, a dropped connection)
  once, and only if nothing had streamed yet.

Endpoints and keys come from the environment:

- Google: `GOOGLE_API_KEY` (or `GEMINI_API_KEY`).
- Ollama: `OLLAMA_HOST`, read the way the `ollama` client reads it.
- OpenRouter: `OPENROUTER_API_KEY`.
- Meta: `META_API_KEY`, with `MODEL_API_BASE` as the base URL.
- `JARVIS_GOOGLE_BASE_URL` and `JARVIS_OPENROUTER_BASE_URL` exist for the
  tests.

`tests/test_edge_llm.py` diffs the edge against the Python path through one
fake provider server: the shaped prompt, the request body, and the record
built from the same reply. Where the edge differs on purpose, the test undoes
the difference by name. Examples: LangChain dropped an assistant's text when it
sat next to tool calls or was stored as a bare string, its tool-schema
conversion was lossy, and it threw away reasoning. Add
to it when adding a provider.

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
| tools | `tools` — bound tools as `core/tool_policy.py` lists them, the SDK from `gql/sdk_tools.json` (Python's catalogue, golden-tested); Python's while any MCP server is configured |
| settings | `settings`, `setting` — the `KNOWN_SETTINGS` registry (`gql/settings.rs`), endpoint API keys redacted |
| memory | `memories`, `memoryActivities`, `memoryUsage`, `agentMemory` (the `AGENTS.md` blob in `kv_store`, the legacy `/AGENTS.md` copied over on first touch) |
| chat page | `models` (endpoint names from `models.endpoints` as providers; keys never sent), `todos` (`thread_state`; a thread not yet converted from `checkpoints.db`, read-only, `src/checkpoints.rs`), `browserAvailable` (an http CDP endpoint) |
| Relay | `node` for every Node type |

Mutations that only write rows and files:

| Domain | Root fields |
|---|---|
| conversations | `updateConversation` (a model checked against the catalog), `deleteConversation`, `discardConversation` |
| task board | `createBoardTask`, `updateBoardTask`, `setBoardTaskStatus`, `answerBoardTask`, `deleteBoardTask` — a card made ready runs a dispatch pass at once; `decomposeBoardTask` — the planner called through `src/llm/` on the task's model, or the whole operation sent to Python when the agent loop is off or doesn't serve that model |
| automations | `createAutomation`, `updateAutomation`, `deleteAutomation` (human callers) — the scheduler reloads at once |
| projects | `createProject`, `updateProject`, `deleteProject`, `setConversationProject` |
| artifacts & documents | `updateArtifact`, `restoreArtifactVersion`, `deleteArtifact`, `deleteDocument` |
| workflows | `createWorkflow`, `updateWorkflow`, `deleteWorkflow` (human callers) |
| lists | `createNotificationChannel`, `updateNotificationChannel`, `deleteNotificationChannel`, `createSkill`, `updateSkill`, `deleteSkill` (human callers) — a skill's description embedded, or saved unembedded if the embedder fails |
| memory | `addMemory` (merged into a near-duplicate), `updateMemoryItem`, `deleteMemory` — embedded by `agent/embed.rs`; an embedder that fails sends the operation to Python before anything is written; `updateMemory`, `deleteAgentMemory` (the blob) |
| models | `addModel`, `updateModel`, `addDiscoveredModels`, `removeModel`, `setDefaultModel`, `addEndpoint`, `updateEndpoint`, `removeEndpoint` (`gql/models.rs`) — `models.custom` / `models.endpoints` rewritten as Python's `json.dumps` writes them; a catalog Python would fail to load, or a stored window it would fail to convert, sent to Python before anything is written; a linked worker re-reads the catalog (`apply_setting`) |
| tools | `setToolPolicy` — only non-default entries stored; Python's while any MCP server is configured; a linked worker drops its policy and agent caches |
| settings | `setSetting`, `deleteSetting` — a managed key overridden or any `mcp.*` key sent to Python before anything is written; an `embedding.model` change re-read by a linked worker (link `call` `apply_setting`) |
| runs (worker linked or owned) | `stopRunningTask`, `stopTask`, `stopAutomationRun`, `stopWorkflowRun`, `stopBoardTask` |
| starting runs (worker linked or owned) | `startTask`, `runWorkflow`, `triggerAutomation` |
| steering runs (worker linked or owned) | `queueMessage`, `unqueueMessage`, `resumeWorkflowRun`, `resolveWorkflowApproval` — through `call` once a worker has the run |
| approvals (worker linked or owned) | `resolveApproval` (a tool gate, a board question, a deferred delete), `requestToolApproval` (the agent's) — see "The agent loop" |

And while a worker is linked, or the edge owns it: every subscription
(`taskEvents`, `automationRunEvents`, `boardTaskEvents`, `workflowRunEvents`)
and `runningTasks`, from the run mirror.

Some are owned per call (`router.rs:Walk::field_rule`): `deleteWorkflow`,
`deleteSkill` and `deleteAutomation` go to Python when the caller is the agent
(`X-Jarvis-Caller: agent` — approval-gated there).

The automation writes (`gql/automation.rs`) port `mutations/automation.py`
with `db/ops.py`'s automation CRUD: a schedule is validated by `cron.rs`
(what `_cron` builds is what fires — "invalid cron expression", as Python
words every refusal), an update writes every field as Python's `setattr` loop
does (one left out is cleared), and a delete takes the runs and a stateful
automation's `automation_{id}` conversation with it. Each one tells the
scheduler to reload (`Scheduler::schedules_changed`) after its commit. **A
change to either side is made in both.**

Deleting a conversation (`conversation.rs:delete_conversation`, shared with
`deleteBoardTask`) ports `db/ops.py:delete_conversation`: the ORM cascades as
explicit DELETEs (messages and their steps, artifacts and their versions,
documents and their chunks, episodes), the transcript thread with the blobs
no other thread names, then the files and the conversation's notebook. **A
change to either is made in both.**

`stopBoardTask` ports `stop_board_task`: the worker or the edge's loop is told
at once, the job is cancelled, and a job no one had claimed yet ends the card
(blocked, "stopped by user") and the mirrored run here.

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
| `modelSync` | provider APIs | the catalog tooling |
| `tools` while an MCP server is configured | loaded MCP tools | MCP |
| `mcpServers`, `mcpTools` | the live `McpManager` | MCP (Phase 2) |
| `voiceStatus` | Piper voice file layout in `core/voice.py` | audio |

| Mutations | Touch | Move with |
|---|---|---|
| `browserActivity` | a running handler's `TaskState`; the agent's kernel is the only caller | the agent loop |
| `setToolPolicy` while an MCP server is configured | the inventory's loaded MCP tools | MCP |
| `addMcpServer`, `updateMcpServer`, `removeMcpServer`, `reloadMcpServers`, `setMcpServerLoadMode`, `setMcpDefaultLoadMode`, `callMcpTool` | the live `McpManager` | MCP |
| `resolveApproval` for an approved MCP call or a paused workflow; either it or `requestToolApproval` for a worker's run (the edge defers those per call) | `call_mcp_tool`; a future in a running workflow; a worker's run stream | the workflow engine, MCP |
| `setSetting`, `deleteSetting` for a managed key overridden (`allowManaged: true`) or any `mcp.*` key (the edge defers those per call) | `apply_setting`'s catalog, tool-policy and MCP caches | the catalog becoming data, MCP |
| `downloadVoice` | the Piper download | audio |
