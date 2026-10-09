# jarvis-edge

The jarvis server and command line, in Rust: the GraphQL API and its live
streams, the agent loop that runs every chat turn, automation, board task and
workflow, the scheduler, the Telegram and Discord bots, MCP, the live browser
view and the REST routes. Only the agent's notebook kernels run Python — the
agent writes Python in `run_cell`, and the `jarvis` SDK it calls there
(`tools/sdk.py`) is Python; it reads the database read-only and asks this
server for everything else.

```
browser / SDK / bots ──▶ jarvis-edge :8000 ──▶ SQLite (database.db)
                                │
                                ├──▶ model providers, MCP servers, the browser (CDP)
                                └──▶ ipykernel processes (the agent's notebooks)
```

It began as an edge in front of the Python server, taking over one operation
at a time; the name stayed. Python's server and runtime are gone (see
`ROADMAP.md`); the module docs that name a Python file say what was ported.

## Running it

```bash
# The server on :8000 (what vite and the jarvis SDK target):
cd edge && JARVIS_APP_DIR=.. cargo run
```

| env | default | |
|---|---|---|
| `JARVIS_EDGE_BIND` | `127.0.0.1:8000` | listen address |
| `JARVIS_EDGE_LOG` | `info` | `error`…`trace` |
| `DATABASE_URL` / `WORK_DIR` | `$WORK_DIR/database.db`, `WORK_DIR` `~/.jarvis` | the database file — the kernels' SDK finds it the same way |
| `ARTIFACTS_DIR` | `$WORK_DIR/artifacts` | artifact files |
| `JARVIS_APP_DIR` | the current directory | the jarvis checkout: the Python kernels run on (`tools/`, `.venv`), and `static/dist`, the SPA served here |
| `JARVIS_KERNEL_PYTHON` | `$JARVIS_APP_DIR/.venv/bin/python`, else `python3` | the interpreter kernels and code automations run on |
| `JARVIS_RUN_JOBS` | on | `0` queues runs without running them — for tests that look at what a write queued |

The built-in model list (`src/builtin_models.json`) and the system prompt
(`src/system_prompt.md`) are compiled in, so rebuild after editing them.

The same binary is the command line (`src/cli/`, see below): `jarvis-edge`
with no command serves, `jarvis-edge start [--host] [--port] [--debug]` too.

What isn't an API route is the SPA: the build's files under
`$JARVIS_APP_DIR/static/dist`, else its `index.html` for the client-side
router (`web.rs`). `/health` answers `{"status":"ok"}`.

## GraphQL (`src/graphql.rs`, `src/gql/`)

`POST /graphql` executes queries and mutations; `GET /graphql` is the
subscription WebSocket (graphql-ws and graphql-transport-ws). The schema is
what the Python (Strawberry) schema was, field for field —
`tests/test_edge_parity.py` checks every type, field and argument it had —
and `frontend/schema.graphql` is exported from it (`pnpm schema`, which runs
`--print-schema`). Introspection is on.

- **A request Strawberry refused before executing** gets its 400 and its
  words: a body that isn't JSON or isn't sent as `application/json`
  (multipart included: uploads aren't enabled), a batch ("Batching is not
  enabled"), a `query`, `variables` or `extensions` of the wrong type, no
  query, an `operationName` naming no operation. A parse or validation error
  is executing's, as in Python.
- **Who is asking**: the `jarvis` SDK sends `X-Jarvis-Caller: agent` and names
  its conversation in `X-Jarvis-Conversation`; everything else is a human. An
  agent's delete of a workflow, skill or automation, and its `callMcpTool`,
  may need a human's approval first (see "Approvals").
- **Wire formats are Python's** (`src/gql/codec.rs`): global ids are
  `base64("Type:id")`, `DateTime` is `isoformat()` (which drops a zero
  fraction), message cursors are urlsafe `base64("{iso}|{id}")`. A write
  leaves a row exactly as SQLAlchemy did: `uuid4()` ids, its stored
  timestamp text, `updated_at` bumped by hand where `onupdate=_now` would
  have, ORM cascades spelled out as explicit DELETEs — including the ones
  that *don't* happen (a memory's access log outlives it; foreign keys are
  off).

## Runs (`src/runs.rs`, `src/gql/start.rs`, `src/gql/runs.rs`)

A run is a `jobs` row the agent loop claims and the run in the registry its
subscribers watch.

- **Starting one** — `startTask`, `runWorkflow`, `triggerAutomation`, a
  schedule firing, a board dispatch, a bot's message — writes the
  conversation, the user message, the row the run reports into and the job
  in one transaction. The run is registered *before* the commit, so a
  subscriber that gets its id back can't miss it, then the loop is woken.
  The run's model is resolved here (`src/catalog.rs`): the built-ins plus
  the `models.custom` and `default.model` settings, a removed model falling
  back to the default.
- **A message for a busy conversation** joins the run already going: a
  `queued` row and a `queued_message` event, taken in before the run's next
  model call. `unqueueMessage` withdraws it until then.
- **Subscriptions** (`taskEvents`, `automationRunEvents`, `boardTaskEvents`,
  `workflowRunEvents`) replay a run's events from the first and follow it to
  its end. Each coerces the raw `{"event", "data"}` records as Python's did
  (`src/gql/events.rs`), with `data.get` / truthiness / `str()` semantics and
  `json.dumps` byte for byte for fields that embed JSON text
  (`src/pyjson.rs`). A run that isn't live answers from its row; a row still
  marked in progress waits up to 2 s for its run first. A finished run
  lingers 5 s for a late subscriber.
- **Stops** set the run's flag, which the turn checks between steps, and the
  job's `cancel_requested`, which the loop polls — so a run stopped before
  it was claimed starts cancelled and finishes as stopped, its rows written.
- `runningTasks` lists the registry.

## The scheduler (`src/schedule.rs`, `src/cron.rs`)

Every timer the Python server ran, ported:

| timer | when | does |
|---|---|---|
| each enabled automation | its cron schedule | enqueues an `automation` job |
| board dispatch | every 15 s, and after a board write that readies a card | `dispatch_board_tasks` |
| memory consolidation | `0 */6 * * *` | the sweep (`src/consolidate/`), if due |
| project memory | every 30 min | the sweep (`src/consolidate/`), if due |
| memory-activity prune | `0 4 * * *` | deletes old access-log rows |

- **Cron is APScheduler's, not a library's.** `cron.rs` ports
  `CronTrigger.from_crontab` (after `normalize_crontab`) with Python's
  `zoneinfo` arithmetic: day-of-month AND day-of-week, a fire time in a
  spring-forward gap keeping its wall clock with the pre-transition offset,
  `fold` in an overlap. `tests/test_edge_schedule.py` holds it to
  APScheduler's recorded answers on 13,200 cases — 14 zones, every 2026 DST transition, 50
  expression shapes — and `Automation.nextRunAt` against Python's.
- **Firing follows APScheduler's job options**: missed runs coalesce, a run
  later than its grace period (60 s for automations) is skipped, and nothing
  missed while the edge was down is caught up.
- **A schedule is re-checked at fire time** against the row, so one disabled
  or deleted a moment ago doesn't fire from a stale copy. Schedules reload
  300 ms after an automation write and every 60 s regardless.
- **The zone** is the `scheduler.timezone` setting, else `JARVIS_TIMEZONE`,
  else `TZ`, else the system zone, else UTC — read at startup, as Python reads
  it.
- **Maintenance waits for work** (`Scheduler::maintenance_due`), because a
  pass calls a model. Each sweep's own first checks, read from the same rows: a message
  past the memory watermark whose first isn't a reply still being written; a
  project with new messages that has been quiet 15 minutes (or waited a day)
  and holds 600+ characters. The watermarks are `kv_store` rows in
  `database.db`. Where unsure (an unreadable watermark, a
  failed read) the answer is yes, and the sweep decides.
  `tests/test_edge_serving.py` holds every gate to the recorded verdict of
  the Python sweep it guards, through `jarvis-edge --maintenance-due`. One thing waits longer: the
  first-run seeding of discrete memory from the old blob now happens with the
  first pass that has a message to read.

## The bots (`src/bots/`)

Ports of Python's `telegram_bot.py` and `discord_bot.py`. Each starts when
its token is set (`TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`).

- **A message is a `startTask`.** It goes through the same `start_chat`
  (`src/gql/start.rs`), with the bot's surface and its conversation id
  (`telegram_<chat>`, `discord_<channel>`): a run already going on that chat
  takes it as a queued message (and the bot says so), otherwise a new run
  starts. The rows are compared with Python's own handlers', recorded
  (`tests/test_edge_bots.py`).
- **The reply follows the run**: `token` events from the main agent, edited
  into the chat at most once a second, created on the first text — never a
  placeholder — and finished when the run is done (or leaves the registry).
- **Telegram** is the Bot API, long-polled; pending updates are dropped at
  start. `TELEGRAM_PROXY_URL` (or `HTTPS_PROXY` / `ALL_PROXY`) proxies it.
- **Discord** is the v10 gateway (heartbeat, zombie detection, resume, and a
  stop on a close no retry fixes, such as 4014 — Message Content Intent off)
  plus REST, retried on 429. Replies never ping anyone.
- **A voice note** is answered "Voice notes aren't supported — send text
  instead." (as Python's bots answer it); nothing transcribes audio.
- **Notifications** (automation and workflow results, `src/notify.rs`) are
  plain Bot API / REST calls with the same tokens — no connected bot needed.
- `TELEGRAM_API_URL`, `DISCORD_API_URL` and `DISCORD_GATEWAY_URL` point the
  bots at another server; the tests' fake uses them.

## The kernels (`src/kernels/`)

The agent's notebooks — one `ipykernel` per session key (a conversation, or a
worker's own key), started on its first cell — are the server's children. A
port of `core/kernels.py`;

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
- **A cell whose run stops** is interrupted; the session's next cell first
  waits for that interrupt to land. A request the kernel receives before it
  has raised is aborted (`stop_on_error`) and would read as no output at all
  — Python's cancel path has that race.
- **`--kernel-cells`** drives the kernels from stdin, one JSON command per
  line (`{key, code, timeout?, conversation_id?, project_id?}` or
  `{shutdown: key}`); `tests/test_edge_kernels.py` compares it with
  `core/kernels.py`'s recorded output.
- **Departures**, named in `tests/test_edge_kernels.py`: stdin is never
  offered, so `input()` raises at once (Python's client offered it with nobody
  to answer, and the cell hung until its timeout).

## MCP (`src/mcp/`)

A port of `core/mcp.py` and of what `langchain_mcp_adapters` does for it;

- **Config** (`config.rs`): `JARVIS_MCP_SERVERS` < the first `mcp.json` that
  names a server (`~/.jarvis/`, then the checkout) < the `mcp.servers`
  setting, then the `mcp.load_modes` overrides; the default mode is the
  `mcp.default_load_mode` setting, else `JARVIS_MCP_DEFAULT_LOAD`, else
  `always`. The shapes and the `transport` guess are `_normalize_servers`'.
- **The client** (`session.rs`, `stdio.rs`, `http.rs`, `ws.rs`) is our own —
  `rmcp` would bring a second `reqwest` and has dropped the HTTP+SSE
  transport. One session per listing and per call, as the adapter opens them:
  `initialize`, `tools/list` (every page) or `tools/call`, close — so a stdio
  server only runs while it is being asked something. stdio runs the server
  in its own process group with `get_default_environment()` plus the config's
  `env` (`${VAR}` expanded), closes stdin, then SIGTERMs the group after 2 s
  and SIGKILLs after 2 more; a session dropped mid-call (a stopped run) kills
  the group. Streamable HTTP POSTs each message and reads a JSON or
  event-stream answer, carries the session id and protocol version, resumes a
  stream that broke off from its last event id, and DELETEs the session on
  close; it opens no standalone GET stream. HTTP+SSE and websocket (the `mcp`
  subprotocol) as Python's SDK speaks them. A key the transport doesn't take
  fails the server, as the `TypeError` does in Python.
- **The manager** (`mod.rs`) lists every server at start (in the background)
  and on each reload, concurrently, a failed one with no tools, five minutes
  at most each (Python waits forever); readers see the old listing until the
  new one is complete. A result becomes the adapter's LangChain blocks
  (`lc_` ids, image/file blocks, `{"structured_content": …}` as the artifact,
  an `isError` as a failed result); `llm_tool` converts a schema as
  `convert_to_openai_tool` does (refs inlined, `$defs` and titles dropped).
- **Bound** (`agent/tools.rs`): an `always` server's tools follow the edge's
  own, keyed for policy as `tool_key_for` keys them (`mcp:<server>/<tool>`),
  gated like any bound tool, run with no timeout; a `lazy` server is named in
  the `mcp_servers` prompt segment (`agent/prompt.rs`).
- **Departures**, named in `tests/test_edge_mcp.py`: a deleted
  `mcp.default_load_mode` falls back at once (Python kept the last one it
  synced until a restart); a tool's structured output isn't validated against
  its output schema (Python's SDK lists the tools again on every call to do
  it).

## The agent loop (`src/agent/`)

Every chat turn, automation run, board task and workflow run.

- **The turn** (`turn.rs`) is `_run_agent_task` and `core/agent_loop.py`:
  the prompt into the thread and the plan reset, then model step, tool
  batch, repeat. Each message is written as it arrives, so a re-claim after
  a crash goes on from the rows (`thread.rs`, the transcript tables). Stops,
  the budget, the step limit, messages queued mid-run, and the leftover
  queue starting the next turn all behave as Python's.
- **The prompt** (`prompt.rs`) is `model_request_node`'s: the system prompt
  (read from the checkout), memory how-to, core and relevant memories, the
  skill catalog (ranked against the request past 8), earlier episodes, the
  live browser, the project, then the todo list or the planning directive.
  Retrieval runs once per user message, as Python's cache does.
- **Retrieval** (`retrieve.rs`, `embed.rs`) ports `core/retrieval.py`
  (FTS5 + cosine, rank fusion, `select_hybrid`'s cutoffs), `search_memory`
  (with its access log), `search_skills`, `search_episodes` and
  `upsert_memory`. The embedder is the one Python picks — Gemini's
  `batchEmbedContents` with `GOOGLE_API_KEY`, else Ollama's `/api/embed`,
  model from `embedding.model` — with Python's query cache.
- **Summarizing** (`summarize.rs`, the pure half in `llm/compact.rs`) ports
  `maybe_compact` and `record_episode`: past the model's threshold, the older
  groups are summarized by the turn's own model (merged into the running
  summary when there is one), the summary replaces them in the thread with
  the step's reply, and the evicted stretch's own summary is stored as an
  episode. History is counted from the last call's usage less the rest of the
  request, except on Ollama; otherwise by chars/4, which is what Python's
  fallback comes to for every provider here but Google (its countTokens API —
  a named departure).
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
  terminating it; webhook runs are one request, 30 s, no redirects.
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
  written; the board budget applies.
- **Stops through the job**: a running job's `cancel_requested` is polled
  every 5 s (`watch_queue_cancel`), so a stop that only reached the job (one
  written before the run was taken, or by another process) still stops the
  run. `stopBoardTask` stops it at once.
- **Tools** (`tools.rs`): the schemas are `tools.json` — first exported from
  Python's own tools, now the source. An unknown tool gets ToolNode's
  error; arguments a tool's signature won't take get `invoke_tool`'s, read
  as Pydantic's lax mode reads them (`tools::Args`: extra keys ignored, an
  integer from a whole float or a numeric string, a boolean from 0/1 or a
  word), so the model can fix its call.
- **Artifacts** (`artifacts.rs`) port `write_artifact`: a markdown body or a
  file the agent wrote (a relative path from the checkout, as Python's
  working directory), the live file plus one copy per version, the rows, and
  the `artifact` event; an artifact from before versioning gets its file
  saved as v1 first. A file's type is `mimetypes.guess_type`'s
  (`src/mimetypes.rs`: Python's built-in table in `mimetypes.json`, then the
  system's `mime.types` files Python reads).
- **Events and steps** (`events.rs`): tokens batched as `TokenCoalescer`
  does, each step's row written before its event.

- **A model the server can't call** (Bedrock with credentials only boto3
  read, a provider nobody configured) fails the run with the reason.
- **A run that can't start** — a chat job without its payload, an
  unreadable thread, an automation of an unknown input type (failed as
  Python's handler fails it), a row or task the database won't give up —
  fails: its row records the error, the run ends `error`, and so does its job
  (`Agent::fail_start`, `queue::fail`). So does a prompt that can't be built
  (`prompt::Unbuilt`: `system_prompt.md` unreadable, a context read failing).
- **Claiming** (`queue.rs`) is `SqliteJobQueue._claim`, under the same
  thread lease, so a conversation's turns run one at a time. The lock is
  renewed at a third of its 300 s TTL; a lost lock abandons the turn. Jobs
  the Python worker queued before it went (`runtime` unset) are claimed like
  any other.
- **Workers** (`src/agent/workers.rs`, a port of `tools/workers.py` and the
  roles in `core/agents.py`): `spawn_workers` runs its tasks at
  once, each on the run's model with its role's prompt and tools (the file
  and artifact tools are `files.rs`, `artifacts.rs`, ports of
  `tools/files.py`, `tools/artifacts.py`), a
  history in memory and a kernel of its own. It answers unknown tools, bad
  arguments (`tools::Args`) and gates itself. Its events reach the turn as notes, written and
  announced in order — all but `worker_token` as `subagent` steps of
  `<role>:<idx>` — and its model and tool calls count against the run's
  budget and usage. A stop drops the workers at once (Python lets an
  in-flight worker call finish and counts it); a batch's calls run in order
  (Python runs them at once).
- **Approvals** (`src/approvals.rs`, a port of `core/tool_gate.py` and
  `core/approval.py`): a call whose policy needs a human's yes
  records an `approvals` row, is shown in the chat (`approval_request`), and
  waits on the row, polled every 1.5 s, for `JARVIS_TOOL_GATE_TIMEOUT` (30
  minutes) before it expires as denied. Every gate in a batch is answered, in
  order, before anything runs; a denied call is answered with the denial.
  The answer comes through `resolveApproval` (`src/gql/approval.rs`): for a
  gate, a board task's question, a paused workflow node, or a deferred
  action. A request no live run is waiting on is closed `expired` with
  Python's "no longer waiting". `requestToolApproval` is the SDK's request
  from a kernel, on the same terms.
- **Deferred actions** (`approval::gate_action`, a port of
  `core/approvals.py:gate_action` and `ACTIONS`): an agent's (`X-Jarvis-Caller:
  agent`) `deleteWorkflow`, `deleteSkill`, `deleteAutomation` or `callMcpTool`,
  when `approval.required_actions` names it (`all`, or a comma list), is
  recorded instead of performed — an `approvals` row with the action and its
  payload, announced on the conversation's live run — and the mutation fails
  with "Approval required: …". An open request for the same action and
  payload is reused, so an agent retrying doesn't fill the inbox. Approving
  runs the action before the row closes, so a failure leaves it answerable;
  denying closes it. Off by default. An agent's `callMcpTool` passes its
  per-tool policy (Settings → Tools) first.
- **Workflows** (`src/agent/workflow/`, ports of `workflow/engine.py`,
  `workflow/nodes.py`, `core/workflow_template.py`,
  `server/workflow_runtime.py` and `tools/workflows.py:run_workflow` — change
  both): `runWorkflow` queues its job. The engine
  runs the graph in
  frontiers, prunes a conditional's or router's unchosen branches, and gives
  each node its retries, timeout and `on_error`; templates are Jinja
  (minijinja, printing values as Python's `str()` does, with Python's
  `tojson`/`fromjson`), Python's regex renderer when Jinja refuses. An agent
  node is the main agent (`agent.rs`): its prompt, retrieval, tools and
  policy, on a history and a kernel of its own, its text streamed as
  `node_token`, its calls counted against the workflow's budget. An approval
  or input node files its `approvals` row (under the run, so the inbox shows
  it) and polls it; `resumeWorkflowRun`, `resolveWorkflowApproval` and
  `resolveApproval` close it with the answer (`workflow::answer`). The
  chat's `run_workflow` runs its workflow inside the call, shown as a worker
  starting and finishing. Named departures: a stop ends the nodes at once
  (Python lets an in-flight model call finish); `output_schema` is not
  checked with `jsonschema` (no `_schema_error`); an agent node's bad tool
  arguments get a plainer error than Pydantic's, and its history is only
  trimmed per call, never summarized; a map item's paused node is filed
  under the run (Python's is nobody's, so unanswerable); a timed-out or
  orphaned request is expired, not left pending; an edge naming no node
  fails the run at once. Tested in `tests/test_edge_workflow.py` (edge only,
  not diffed).
- **Recovery**: at start the jobs a previous process left running are
  pending again, then what a crash left behind is swept up (`sweep.rs`,
  ports of Python's startup sweeps): a message, automation run or workflow
  run still `running` with no live job is an error, a board task `running`
  with none is `ready` again (`cleanup_zombie_running_rows`); a pending tool
  gate or paused node is `expired` — its waiter died, and a re-claimed run
  asks again (`reconcile_startup`); and incognito conversations no live chat
  job belongs to are deleted (`sweep_ephemeral_conversations`).
  `--startup-sweep` does just this and exits, for the tests.

Tested in `tests/test_edge_agent.py` (the queue, recovery, runs that can't
start) and `tests/test_edge_loop.py`, which runs scripted turns against a
fake Ollama and compares the events a subscriber gets, the step rows, the
message, the thread and every model request against Python's runs of the
same turns, recorded. Departures are named there.

## The LLM layer (`src/llm/`)

Model calls, which the agent loop (`src/agent/`) makes.
`--llm-shape` and `--llm-call` drive it alone, reading one request as JSON
on stdin.

- `transcript.rs` — the v1 record (`edge/transcript_format.md`). A row Python
  wrote reads and writes back equal, `null`s included.
- `shape.rs` — a port of `core/messages.py` (`strip_historical_thinking`,
  `repair_orphan_tool_calls`, `build_llm_messages`) and the cache layout in
  `core/context_cache.py`. The
  result is a `Prompt` with breakpoints as flags; each provider spells them.
- One module per wire format, hand-written. No client library sits above
  our request builder, so the request is ours byte for byte. Each renders a
  `Prompt`, streams the reply (text and thinking deltas) and builds the
  assistant record:
  - `anthropic.rs`: the Messages API, for `anthropic`, as `ChatAnthropic`
    (langchain-anthropic) sends it: user and tool turns merged into one user
    message, a tool result as a `tool_result` block (its breakpoint hoisted
    onto it), blank text left out, another provider's tool-call ids hashed
    to `toolu_…` as LangChain does, `max_tokens` from langchain-anthropic's
    model profiles (4096 for a model they don't list), `"ttl": "1h"` on the
    breakpoints with `JARVIS_CACHE_TTL=1h`. Departures: a stored PDF goes as
    a `document` block (LangChain sent a block Anthropic
    refuses), and the stop reason is kept as the finish reason.
  - `bedrock.rs`: ConverseStream, for `bedrock`, as `ChatBedrockConverse`
    (langchain-aws) sends it through boto3, signed by `src/aws.rs` (service
    `bedrock`, the endpoint from `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` /
    `AWS_ENDPOINT_URL`, else the region's). Consecutive user or assistant
    messages merged as `merge_message_runs` merges them, a tool result as a
    `toolResult` in the user turn, a blank text as `"."`, signed thinking as
    `reasoningContent`, breakpoints as `cachePoint` blocks, tool schemas
    with their null branches stripped. The reply is AWS event-stream frames,
    CRC-checked. Departures: the reasoning is recorded as thinking (LangChain
    kept it opaque), the provider as `bedrock`, the stop reason as the finish
    reason. A turn whose AWS credentials come from a source only boto3 read
    (assume-role, SSO, web identity, `credential_process`, a container role)
    fails with the reason.
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

- Anthropic: `ANTHROPIC_API_KEY`, with `ANTHROPIC_API_URL` (else
  `ANTHROPIC_BASE_URL`) as the base URL, as langchain-anthropic reads them.
- Bedrock: boto3's chain as `src/aws.rs` reads it (environment keys, the
  shared files' static keys, the instance role), the region from
  `AWS_REGION` / `AWS_DEFAULT_REGION` (else `us-east-1`).
- Google: `GOOGLE_API_KEY` (or `GEMINI_API_KEY`).
- Ollama: `OLLAMA_HOST`, read the way the `ollama` client reads it.
- OpenRouter: `OPENROUTER_API_KEY`.
- Meta: `META_API_KEY`, with `MODEL_API_BASE` as the base URL.
- `JARVIS_GOOGLE_BASE_URL` and `JARVIS_OPENROUTER_BASE_URL` exist for the
  tests.

`tests/test_edge_llm.py` compares the edge with what the Python path
(LangChain) did through one fake provider server, recorded: the shaped
prompt, the request body, and the record built from the same reply. Where the edge differs on purpose, the test undoes
the difference by name. Examples: LangChain dropped an assistant's text when it
sat next to tool calls or was stored as a bare string, its tool-schema
conversion was lossy, and it threw away reasoning. Add
to it when adding a provider.

## The command line (`src/cli/`)

A port of `main.py`: `run "<query>" [--model] [--no-save]
[--debug]`, `config set|get|list|delete`, `model list|add|remove|set-default|sync`,
`memory show|set|reset`, `start`, and a global `--work-dir`. Arguments are
clap's; the test hooks (`--print-schema`, `--cron-next`, `--llm-call`, …) keep
their raw flags and skip it.

- **The same code as the server.** `config` writes `config_settings` raw, as
  `db/ops.py` does (no validation; a running server reads the row when it next
  needs it). `model` goes through `catalog.rs` and `gql/models.rs`'s
  custom-model helpers; `model sync` through `discovery.rs`, every listing and
  probe gathered before anything prints. `memory` reads and writes the
  `AGENTS.md` row as `KvStore` does — `set` stores `{"content": …}` only, and
  `updated_at` moves only when the value changes. `run` is the chat agent in
  memory (`agent::workflow::run_once`, the workflow agent node's loop, last
  reply returned): no conversation, its own kernel, shut down after; the
  prompt is read from `JARVIS_APP_DIR`.
- **Failures say why** where Python would have answered something: a
  `models.custom` row that won't load, an `AGENTS.md` row or file that isn't
  text; `run` on a model it can't call fails the call. Every command first
  creates or migrates the database (`schema.rs`), as `main.py`'s `init_db`
  does, and `memory *` copies the LangGraph store over first, once, as
  `main.py` does.
- **Output.** The same words as `main.py`, plain: tables are aligned columns,
  a panel is its title and text, `run` prints the reply's Markdown as is.
  Colour only on a terminal without `NO_COLOR`. `memory reset` asks
  `[y/N]` as `typer.confirm` does.
- **Departures.** Rich wraps at the console width and swallows `[text]` it reads as
  markup; the edge does neither. Not ported: `reports`/`view` (nothing writes
  `reports/` any more). `download-voice` and `maintenance *` are gone from
  both: voice was removed, and the maintenance commands converted
  `checkpoints.db`.
- Compared with `main.py`'s recorded output, exit codes and rows in
  `tests/test_edge_cli.py`, with `run` on the fake Ollama of
  `test_edge_loop.py` and `model sync` on the fake providers of
  `test_edge_model_sync.py`.

## The database, files and routes

- **The schema** (`src/schema.rs`). At start, and before a command-line
  command, it creates every table missing from `schema.sql` (first captured
  from Python's `create_all`), then runs `migrate` (columns, indexes, the
  artifact backfill, the FTS5 mirrors — a port of `db/engine.py:_migrate`)
  and the one-time LangGraph store import. A schema change goes in
  `schema.sql` and `migrate`; `tests/test_edge_schema.py` checks a migrated
  database has the fresh shape. The kernels' SDK reads the same file
  (read-only, plain `sqlite3`). The file is opened with Python's pragmas,
  plus `foreign_keys = OFF`, which sqlx would otherwise turn on.
- **The SDK catalogue** the `tools` query lists (`src/gql/sdk_tools.json`) is
  exported from `tools/sdk.py` — re-export after changing an SDK function
  (`JARVIS_UPDATE_GOLDEN=1 uv run pytest tests/test_edge_parity.py -k catalogue`).
- **REST** (`src/rest.rs`, `src/logs.rs` — ports of `routes_artifacts.py` and
  `routes_logs.py`). Downloads answer as Starlette's `FileResponse` did: its
  headers (ETag = md5 of `"{st_mtime}-{size}"`), one byte range, `HEAD` a
  405; several ranges, or a range number only `int()` reads, get the whole
  file, and a path that isn't a file is missing. The log viewer is the
  server's `tracing` events as Python's handler shaped records, refused to a
  cross-origin page and to anything but a loopback peer. Compared in
  `tests/test_edge_rest.py`.
- **`/ws/browser`** (`src/browser/` — ports of `routes_browser.py`,
  `core/browser_stream.py` and `tools/browser.py`'s `ensure_running`; change
  both). The server finds the browser (or launches one, as the kernel would),
  attaches its own CDP client to the tab Playwright calls `pages[0]` (the
  first page `Target.setAutoAttach` reports), and fans the screencast out —
  one cast while anyone watches, newest frame only, the last frame for a late
  joiner. The messages are Python's, byte for byte. Departures: a closed tab
  or browser ends the stream with `unavailable`/`"the browser went away"`
  (Python kept sending `idle`), and the next viewer attaches afresh; an attach
  that runs out its 15 s says so (Python sent an empty reason); a page in a
  non-default browser context isn't skipped. Tested against a fake DevTools
  browser in `tests/test_edge_browser.py`.

## Tests

`tests/test_edge_*.py` drive the binary. Most diff it against what the Python
server answered for the same scenario, recorded while it existed
(`tests/python_golden.py`, `tests/golden/python/`): an operation's answer,
every table and artifact file after a mutation, a run's events, rows and
model requests. A deliberate change to an answer is a change to the
recording, made by hand and said in the commit. Some still diff against
Python code that remains — the LLM request shaping (`test_edge_llm.py`), the
kernels (`test_edge_kernels.py`), the CLI (`test_edge_cli.py`), the cron
engine (`test_edge_schedule.py`), the schema (`test_edge_schema.py`) — and
`cargo test` runs the unit tests.

Test hooks keep raw flags and skip the CLI: `--print-schema`,
`--cron-next`, `--guess-type`, `--llm-shape`, `--llm-call`,
`--maintenance-due`, `--maintenance-run`, `--init-db`, `--startup-sweep`,
`--kernel-cells`, `--replay-events`.

## What stays in Python

The agent's notebook kernels, and what they import: the `jarvis` SDK
(`tools/sdk.py`), `tools/research.py` and `tools/browser.py`, and the parts of
`core/` and `db/` those reach. Python's agent runtime — `core/agents.py`, the
loop, the workflow engine, the Python CLI — no longer serves anything; it
stays only while tests diff against it, and goes with the decision on what
the kernel keeps (`ROADMAP.md`).
