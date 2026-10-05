# workflow/ — graph executor

- **Ported to the edge** (`edge/src/agent/workflow/`): the engine, every node type, the templates, the job handler and `run_workflow` — a change here is made in both. The edge runs a workflow when every model its graph can call is one it calls; otherwise Python does. See `edge/README.md` → Workflows for the named departures.
- Definitions are JSON (`Workflow.definition`: `nodes` + `edges`). `engine.py:execute_workflow(run_id, definition, inputs, task_state)` runs BFS; `server/workflow_runtime.py` is the job handler.
- Node types (`nodes.py`, `NODE_REGISTRY`): `start`, `agent`, `conditional`, `router`, `map`, `refine`, `sequential`, `parallel`, `loop`, `approval`, `human_input`, `planner` (alias `plan`).
- **Adding a node type**: subclass `BaseNode` with `node_type` and `execute()`, register in `NODE_REGISTRY`, emit events with `_emit(task_state, "event", **data)` — never append to `task_state.events`. Nodes that call an LLM follow the sanitization rule in the root CLAUDE.md.
- **Per-node resilience** (handled in `engine.py:_run_node`): `timeout_seconds`, `retries` (0–10), `retry_delay_seconds`, `on_error` (`error` | `continue` | `skip`), `fallback_output`. Emits `node_retry` between attempts; checks `task_state.cancelled` while sleeping.
- **Structured output**: `agent` and `sequential` steps accept `output_schema`; `_extract_first_json()` pulls the first JSON value and dict keys become outputs. `output_schema_mode="strict"` raises when none is found.
- **Templates** (`core/workflow_template.py`): Jinja2 when installed, regex fallback. `{{var}}` = `{{inputs.var}}`, `{{nodes.<id>.<key>}}`, `{{workflow.<key>}}`; filters `upper`, `lower`, `trim`, `default`, `tojson`/`json`, `fromjson`. The engine sets a ContextVar before each node so `_interpolate` resolves `{{nodes.*}}`.
- Conditional nodes prune inactive branches via `pruned_edges`.
- **Human-in-the-loop**: `approval` / `human_input` pause via `TaskState` interrupt + `resume_future`; resumed by `resumeWorkflowRun` / `resolveWorkflowApproval`. Run state is in memory only, so a restart re-runs the workflow from the start and expires its approval rows. (The edge's nodes wait on their `approvals` row instead; its resolvers close the row.)
- `run_workflow` (bound tool, `tools/workflows.py`) runs a saved workflow as a sub-agent.
- Events: `node_start`, `node_token`, `node_condition`, `node_done`, `node_error`, `node_retry`, `map_start`, `map_item_done`, `workflow_done`, `workflow_error`, plus the approval/interrupt events.
