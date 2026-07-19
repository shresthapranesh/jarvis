"""Workflow node classes.

Each node type encapsulates its own execution logic. Nodes communicate
through a shared outputs dict keyed by (node_id, port_name) — the engine
resolves each node's inputs by following incoming edges.
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from core.agents import build_agent
from core.model_catalog import DEFAULT_MODEL, get_model_spec
from core.state import TaskState, emit_event as _emit
from core.streaming import STREAM_MODES


# ── NodeOutput ────────────────────────────────────────────────────────────────

@dataclass
class NodeOutput:
    """Result produced by a node after execution.

    data:         keyed by output port name, values are the produced data
    next_handles: for ConditionalNode — which sourceHandles are active;
                  None means all outgoing edges are active (default)
    """
    data: dict[str, Any]
    next_handles: list[str] | None = None


# ── BaseNode ──────────────────────────────────────────────────────────────────

class BaseNode(ABC):
    """Abstract base for all workflow node types.

    Subclasses must declare a ``node_type`` class variable and implement
    ``execute()``.
    """

    node_type: str  # overridden by each subclass

    def __init__(self, node_id: str, config: dict[str, Any]) -> None:
        self.node_id = node_id
        self.config = config

    @abstractmethod
    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        """Execute this node.

        Args:
            inputs:     dict keyed by input port name; values resolved from
                        upstream node outputs via incoming edges
            task_state: live TaskState used to emit SSE events

        Returns:
            NodeOutput with data and optional next_handles
        """
        ...

    def input_ports(self) -> list[str]:
        return list(self.config.get("input_ports", []))

    def output_ports(self) -> list[str]:
        return list(self.config.get("output_ports", []))


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _interpolate(
    template: str,
    inputs: dict[str, Any],
    completed: dict[str, Any] | None = None,
    workflow_inputs: dict[str, Any] | None = None,
) -> str:
    """Replace ``{{var}}`` placeholders — now Jinja-backed with {{nodes.*}} support.

    Backward compat: _interpolate(template, inputs) still works,
    pulling completed/workflow from the ContextVar set by the engine.
    """
    try:
        from core.workflow_template import render_template

        return render_template(template, inputs, completed, workflow_inputs)
    except Exception:
        # Ultimate fallback — legacy regex
        def replace(m: re.Match) -> str:
            key = m.group(1).strip()
            return str(inputs.get(key, m.group(0)))

        return re.sub(r"\{\{(.+?)\}\}", replace, template)


def _get_model_spec(model_id: str):
    """Return ModelSpec for model_id, falling back to DEFAULT_MODEL."""
    try:
        return get_model_spec(model_id)
    except ValueError:
        return get_model_spec(DEFAULT_MODEL)


def _extract_tokens(content: Any) -> str:
    """Extract plain text from an AI token content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _block_text(content: Any) -> str:
    """Collapse an LLM response's ``.content`` (str or list of blocks) to text.

    Reasoning models return a list of typed blocks; single-turn callers
    (router / evaluator) only want the text portion.
    """
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return str(content).strip()


async def _run_agent_text(
    node_id: str, model_id: str, prompt: str, task_state: TaskState
) -> str:
    """Run the full agent loop on ``prompt``, streaming main-agent tokens as
    ``node_token`` events, and return the accumulated final text.

    Shared by AgentNode and RefineNode. Each call uses a fresh checkpointer
    thread so independent invocations never share history; history hygiene is
    handled inside ``build_agent``'s model node.
    """
    from core.runner import build_callbacks
    from core.state import get_async_checkpointer, get_store
    from langchain_core.runnables import RunnableConfig

    # Budget tracking — if the workflow run has a tracker (set by workflow_runtime),
    # piggyback on it so token limits are enforced per-workflow, not per-node.
    bt = getattr(task_state, "_budget_tracker", None)
    callbacks: list = build_callbacks(bt, task_state=task_state)

    agent = build_agent(
        model=model_id,
        checkpointer=get_async_checkpointer(),
        store=get_store(),
    )
    run_config: RunnableConfig = {
        "configurable": {"thread_id": str(uuid4())},
        "recursion_limit": 100,
        "callbacks": callbacks,
    }
    final_text = ""
    async for raw_chunk in agent.astream(
        {"messages": [{"role": "user", "content": prompt}]},
        config=run_config,
        stream_mode=STREAM_MODES,
        subgraphs=True,
    ):
        ns, mode, data = raw_chunk
        if mode != "messages":
            continue
        token, _ = data
        if getattr(token, "type", "") not in ("ai", "AIMessageChunk"):
            continue
        if not hasattr(token, "content"):
            continue
        # Only accumulate main-agent tokens (ns is None/empty), not subagent tokens
        if ns:
            continue
        text = _extract_tokens(token.content)
        if text:
            final_text += text
            _emit(task_state, "node_token", node_id=node_id, text=text)
    return final_text


def _match_category(response: str, categories: list[str]) -> str:
    """Map a free-text routing response to one of ``categories``.

    Robust across providers (no structured-output dependency): tries exact /
    prefix match, then containment (longest name first so a longer category
    isn't shadowed by a shorter substring), then falls back to the first
    category.
    """
    text = response.strip().lower()
    for c in categories:
        if text == c.lower() or text.startswith(c.lower()):
            return c
    for c in sorted(categories, key=len, reverse=True):
        if c.lower() in text:
            return c
    return categories[0]


def _extract_first_json(text: str) -> Any | None:
    """Try to extract first JSON object/array from text."""
    if not text:
        return None
    # Fast path: whole text is JSON
    try:
        return json.loads(text)
    except Exception:
        pass
    # Find first { or [
    start = text.find("{")
    bracket = text.find("[")
    if bracket != -1 and (start == -1 or bracket < start):
        start = bracket
    if start == -1:
        return None
    # Try to parse from start, shrinking
    for end in range(len(text), start, -1):
        snippet = text[start:end]
        try:
            return json.loads(snippet)
        except Exception:
            continue
    return None


def _maybe_parse_structured_output(
    final_text: str, output_schema: Any | None
) -> tuple[Any, str | None]:
    """If output_schema provided, try to parse JSON from final_text.

    Returns (parsed_or_original_text, error_message_or_None).
    If no schema, returns (final_text, None).
    """
    if not output_schema:
        return final_text, None
    # Try to extract JSON
    parsed = _extract_first_json(final_text)
    if parsed is None:
        return final_text, "Failed to extract JSON matching output_schema"
    # Optional: validate against JSON schema if jsonschema lib available — best effort
    try:
        if isinstance(output_schema, str):
            schema_obj = json.loads(output_schema)
        else:
            schema_obj = output_schema
        # If jsonschema is installed, validate
        try:
            import jsonschema  # type: ignore

            jsonschema.validate(parsed, schema_obj)
        except ImportError:
            pass
        except Exception as ve:
            return parsed, f"JSON schema validation warning: {ve}"
    except Exception:
        # If schema itself invalid, ignore
        pass
    return parsed, None


# ── AgentNode ─────────────────────────────────────────────────────────────────

class AgentNode(BaseNode):
    """Runs the full agentic loop (web search, code execution, etc.) with a
    prompt template rendered from its inputs.

    Config fields:
        prompt_template (str):  template with ``{{var}}`` placeholders
        model (str):            model id, defaults to DEFAULT_MODEL
        output_key (str):       name of the output port, defaults to "result"
        input_ports (list[str]): expected input port names
        output_schema (dict | str | None): JSON schema for structured output.
            If provided, the prompt is augmented to request JSON and the result
            is parsed. The parsed object is returned under output_key, with raw
            text under f"{output_key}_raw" if parsing succeeded.
        output_schema_mode (str): "auto" (default) — try to parse JSON, fallback
            to text; "strict" — raises if JSON not found.
    """

    node_type = "agent"

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        prompt = _interpolate(self.config.get("prompt_template", ""), inputs)
        model_id = self.config.get("model", DEFAULT_MODEL)
        output_key = self.config.get("output_key", "result")
        output_schema = self.config.get("output_schema")
        schema_mode = self.config.get("output_schema_mode", "auto")

        # Augment prompt if schema provided
        if output_schema:
            schema_str = output_schema if isinstance(output_schema, str) else json.dumps(output_schema, indent=2)
            prompt = (
                f"{prompt}\n\n"
                f"You must output valid JSON matching this JSON schema (or shape):\n"
                f"{schema_str}\n\n"
                f"Output ONLY the JSON, no extra explanation, no markdown fences."
            )

        final_text = await _run_agent_text(self.node_id, model_id, prompt, task_state)

        if output_schema:
            parsed, err = _maybe_parse_structured_output(final_text, output_schema)
            if isinstance(parsed, (dict, list)):
                data: dict[str, Any] = {output_key: parsed, f"{output_key}_raw": final_text}
                # If parsed is dict, also merge its keys as top-level outputs for convenience
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        if k not in data:
                            data[k] = v
                if err:
                    data[f"{output_key}_schema_error"] = err
                _emit(
                    task_state,
                    "node_token",
                    node_id=self.node_id,
                    text=f"\n— structured output parsed ({type(parsed).__name__}) —\n",
                )
                return NodeOutput(data=data)
            else:
                if schema_mode == "strict":
                    raise ValueError(f"AgentNode {self.node_id}: structured output required but no JSON found. Raw: {final_text[:500]}")
                # Fallback to text
                return NodeOutput(data={output_key: final_text, f"{output_key}_parse_error": err or "no json"})
        return NodeOutput(data={output_key: final_text})


# ── ConditionalNode ───────────────────────────────────────────────────────────

class ConditionalNode(BaseNode):
    """Routes execution to either the ``true`` or ``false`` output port by
    asking an LLM a yes/no question evaluated against the incoming data.

    Uses a single-turn LLM call (no agent loop, no tools) for fast, cheap
    routing decisions.

    Config fields:
        condition (str):  question/statement with ``{{var}}`` placeholders;
                          LLM answers "true" or "false"
        model (str):      model id for evaluation, defaults to DEFAULT_MODEL
        input_key (str):  primary input port name (used by output_ports)
    """

    node_type = "conditional"

    def output_ports(self) -> list[str]:
        return ["true", "false"]

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        from langchain_core.messages import HumanMessage, SystemMessage

        condition = _interpolate(self.config.get("condition", ""), inputs)
        model_id = self.config.get("model", DEFAULT_MODEL)

        spec = _get_model_spec(model_id)
        llm = spec.build_llm()

        response = await llm.ainvoke([
            SystemMessage(content="You are a routing assistant. Answer ONLY with 'true' or 'false'. No explanation."),
            HumanMessage(content=condition),
        ])

        raw = response.content
        if isinstance(raw, list):
            # Reasoning models return a list of blocks
            raw = " ".join(
                b.get("text", "") for b in raw
                if isinstance(b, dict) and b.get("type") == "text"
            )
        verdict = str(raw).strip().lower().startswith("true")
        active_handle = "true" if verdict else "false"

        _emit(task_state, "node_condition", node_id=self.node_id, verdict=active_handle)

        return NodeOutput(data=inputs, next_handles=[active_handle])


# ── MapNode ───────────────────────────────────────────────────────────────────

class MapNode(BaseNode):
    """Applies a sub-workflow to each item in a list and collects the results.

    Supports two modes (mutually exclusive; workflow_id takes priority):
        Option A — workflow_id: references a saved Workflow record in the DB
        Option B — sub_graph:   inline {"nodes": [...], "edges": [...]} dict

    Config fields:
        items_key (str):   input key whose value is the list to iterate over
        workflow_id (str): Option A — id of a saved Workflow
        sub_graph (dict):  Option B — inline graph definition
        result_key (str):  output port name, defaults to "results"
        concurrency (int): max parallel executions; None means fully concurrent
    """

    node_type = "map"

    def output_ports(self) -> list[str]:
        return [self.config.get("result_key", "results")]

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        from workflow.engine import execute_workflow  # lazy import — avoids circular dep

        items_key = self.config["items_key"]
        result_key = self.config.get("result_key", "results")
        items = inputs.get(items_key, [])
        total = len(items)

        definition = await self._resolve_definition()

        _emit(task_state, "map_start", node_id=self.node_id, total=total)

        async def run_item(index: int, item: Any) -> Any:
            item_inputs = {**inputs, "item": item, "index": index}
            child_run_id = f"{self.node_id}_item_{index}"
            child_state = TaskState()
            outputs, _ = await execute_workflow(child_run_id, definition, item_inputs, child_state)

            _emit(task_state, "map_item_done", node_id=self.node_id, index=index, result=outputs)
            return outputs

        concurrency = self.config.get("concurrency")
        if concurrency:
            sem = asyncio.Semaphore(int(concurrency))

            async def run_limited(i: int, item: Any) -> Any:
                async with sem:
                    return await run_item(i, item)

            results = await asyncio.gather(*[run_limited(i, item) for i, item in enumerate(items)])
        else:
            results = await asyncio.gather(*[run_item(i, item) for i, item in enumerate(items)])

        return NodeOutput(data={result_key: list(results)})

    async def _resolve_definition(self) -> dict:
        workflow_id = self.config.get("workflow_id")
        sub_graph = self.config.get("sub_graph")

        if workflow_id:
            from db.engine import async_session
            from db.ops import get_workflow
            async with async_session() as session:
                wf = await get_workflow(session, workflow_id)
            if wf is None:
                raise ValueError(f"MapNode: workflow {workflow_id!r} not found")
            return json.loads(wf.definition)

        if sub_graph:
            return sub_graph

        raise ValueError("MapNode requires either 'workflow_id' or 'sub_graph' in config")


# ── PlannerNode (ADK planning analog) ─────────────────────────────────────

class PlannerNode(BaseNode):
    """Planning node — produces a structured plan (list of steps) from a goal.

    Uses a single-turn LLM call (no tool loop) for fast, cheap planning.
    Emits ``node_token`` for each planned step and returns the plan as list.

    Config fields:
        prompt_template (str): goal / task with {{var}} placeholders
        rubric (str): optional guidance / constraints for planning
        model (str): model id, defaults to DEFAULT_MODEL
        max_steps (int): max steps (default 5, clamped 1..10)
        output_key (str): output port name, defaults to "plan"
        output_schema (dict|str|None): optional schema for structured plan output
    """

    node_type = "planner"

    def output_ports(self) -> list[str]:
        return [self.config.get("output_key", "plan"), "result", f"{self.config.get('output_key','plan')}_text"]

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        from langchain_core.messages import HumanMessage, SystemMessage

        base_prompt = _interpolate(str(self.config.get("prompt_template", self.config.get("goal", "") or "")), inputs)
        rubric = _interpolate(str(self.config.get("rubric", "") or ""), inputs)
        model_id = self.config.get("model", DEFAULT_MODEL) or DEFAULT_MODEL
        output_key = self.config.get("output_key", "plan")
        max_steps = max(1, min(10, int(self.config.get("max_steps", 5))))
        output_schema = self.config.get("output_schema")

        instruction = (
            f"You are a planning assistant. Given the goal, break it into exactly {max_steps} "
            f"or fewer concrete, actionable steps. Each step should be a short sentence (10-15 words). "
            "Return ONLY a JSON array of strings — no explanation, no markdown, no numbering inside strings."
        )
        if rubric:
            instruction += f"\n\nGuidance / constraints:\n{rubric}"

        # If output_schema requested, include it
        if output_schema:
            schema_str = output_schema if isinstance(output_schema, str) else json.dumps(output_schema, indent=2)
            instruction += f"\n\nOutput must match JSON schema:\n{schema_str}"

        llm = _get_model_spec(model_id).build_llm()

        response = await llm.ainvoke(
            [
                SystemMessage(content=instruction),
                HumanMessage(content=base_prompt or "Plan the steps to accomplish the inputs"),
            ]
        )
        raw_text = _block_text(response.content)

        # Try structured parse first if schema present
        parsed: Any = None
        if output_schema:
            parsed_try, _err = _maybe_parse_structured_output(raw_text, output_schema)
            if isinstance(parsed_try, (dict, list)):
                parsed = parsed_try

        # Try to extract JSON array
        plan_list: list[str] = []
        if parsed is None:
            extracted = _extract_first_json(raw_text)
            if isinstance(extracted, list):
                plan_list = [str(x).strip() for x in extracted if str(x).strip()]
            elif isinstance(extracted, dict):
                # If dict contains steps/plans key
                for key in ("steps", "plan", "tasks", "items"):
                    if key in extracted and isinstance(extracted[key], list):
                        plan_list = [str(x).strip() for x in extracted[key] if str(x).strip()]
                        break

        # Fallback: split raw_text by newlines / numbers
        if not plan_list:
            # Split on numbered or bullet lines
            lines = re.split(r"\r?\n", raw_text.strip())
            candidates: list[str] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                # Remove leading numbering/bullet
                line = re.sub(r"^\s*(?:\d+[\.\)]\s*|[-*]\s+|step\s*\d+[:\.]\s*)", "", line, flags=re.IGNORECASE).strip()
                # Strip quotes/brackets
                line = line.strip('"\'' )
                if line and len(line) > 5:
                    candidates.append(line)
            if candidates:
                plan_list = candidates[:max_steps]
            else:
                # Last resort: single plan = raw text
                plan_list = [raw_text[:500]]

        # Clamp to max_steps
        plan_list = plan_list[:max_steps]

        # Emit tokens for visibility
        for idx, step in enumerate(plan_list, 1):
            _emit(task_state, "node_token", node_id=self.node_id, text=f"{idx}. {step}\n")

        data: dict[str, Any] = {
            output_key: plan_list,
            f"{output_key}_text": "\n".join(f"{i+1}. {s}" for i, s in enumerate(plan_list)),
            "result": plan_list,
        }
        # If structured schema parsed and it's dict/list, merge
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                if k not in data:
                    data[k] = v
        elif isinstance(parsed, list) and output_key not in data:
            data[output_key] = parsed

        return NodeOutput(data=data)


# ── StartNode ─────────────────────────────────────────────────────────────────

class StartNode(BaseNode):
    """Explicit workflow entry point.

    Outputs the workflow-level runtime inputs merged over the default values
    defined in ``initial_inputs``.  The engine already injects the runtime
    inputs into start nodes (nodes with no incoming edges), so this node
    simply merges defaults → runtime and passes them through.

    Config fields:
        initial_inputs (dict[str, str]): default key→value pairs.
                                         Runtime inputs override these defaults.
    """

    node_type = "start"

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        defaults = self.config.get("initial_inputs", {})
        return NodeOutput(data={**defaults, **inputs})


# ── RouterNode ──────────────────────────────────────────────────────────────

class RouterNode(BaseNode):
    """Multi-way classifier — routes execution to exactly one of several named
    categories, pruning all the other branches.

    Generalizes ConditionalNode (two-way true/false) to N labeled output ports.
    Uses a single-turn LLM call with robust free-text matching rather than
    structured output, so it stays reliable on Ollama/Gemma backends that
    handle JSON/tool-call coercion poorly.

    Config fields:
        categories (list[str]): output port names; the LLM picks exactly one.
                                Outgoing edges use these as their sourceHandle.
        instruction (str):      classification prompt with ``{{var}}`` placeholders
        model (str):            model id, defaults to DEFAULT_MODEL
    """

    node_type = "router"

    def output_ports(self) -> list[str]:
        return [str(c) for c in self.config.get("categories", [])]

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        from langchain_core.messages import HumanMessage, SystemMessage

        categories = [str(c) for c in self.config.get("categories", [])]
        if not categories:
            raise ValueError("RouterNode requires a non-empty 'categories' list")

        instruction = _interpolate(self.config.get("instruction", ""), inputs)
        model_id = self.config.get("model", DEFAULT_MODEL)
        llm = _get_model_spec(model_id).build_llm()

        cat_list = ", ".join(categories)
        response = await llm.ainvoke([
            SystemMessage(content=(
                "You are a routing classifier. Choose the single best-matching "
                "category for the input. Answer with ONLY the category name, "
                f"exactly as written, chosen from: {cat_list}. No explanation."
            )),
            HumanMessage(content=instruction),
        ])
        chosen = _match_category(_block_text(response.content), categories)

        _emit(task_state, "node_condition", node_id=self.node_id, verdict=chosen)
        return NodeOutput(data=inputs, next_handles=[chosen])


# ── RefineNode ──────────────────────────────────────────────────────────────

async def _evaluate_draft(
    llm: Any, rubric: str, task: str, draft: str
) -> tuple[bool, str]:
    """Single-shot evaluator: judge ``draft`` against ``rubric``.

    Returns ``(passed, critique)``. Robust free-text parse (PASS/FAIL as the
    leading token) so it works without structured output.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    response = await llm.ainvoke([
        SystemMessage(content=(
            "You are a strict reviewer. Judge whether the draft satisfies the "
            "criteria. Respond with 'PASS' or 'FAIL' as the very first word. If "
            "FAIL, follow it with specific, actionable critique of what to fix."
        )),
        HumanMessage(content=(
            f"Criteria:\n{rubric or '(none given — judge overall quality and correctness)'}\n\n"
            f"Original task:\n{task}\n\n"
            f"Draft:\n{draft}"
        )),
    ])
    text = _block_text(response.content)
    low = text.lower()
    passed = low.startswith("pass")
    critique = text
    for tok in ("pass", "fail"):
        if low.startswith(tok):
            critique = text[len(tok):].lstrip(" :.-\n")
            break
    return passed, critique


class RefineNode(BaseNode):
    """Evaluator-optimizer loop — generate, evaluate against a rubric, and
    revise until a reviewer LLM accepts the draft or ``max_iterations`` is hit.

    Self-contained (loops internally) so it needs no cycles in the DAG engine.
    Generation runs the full agent loop (tools available); evaluation is a
    cheap single-turn call. Streams generation tokens as ``node_token`` and
    surfaces each round's verdict as a short ``node_token`` marker.

    Config fields:
        prompt_template (str):  generation task with ``{{var}}`` placeholders
        rubric (str):           criteria the evaluator judges against
        model (str):            model id for generation + evaluation
        max_iterations (int):   max generate/evaluate rounds (default 3, clamped 1..5)
        output_key (str):       output port name (default "result")
    """

    node_type = "refine"

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        base_prompt = _interpolate(self.config.get("prompt_template", ""), inputs)
        rubric = _interpolate(self.config.get("rubric", ""), inputs)
        model_id = self.config.get("model", DEFAULT_MODEL)
        output_key = self.config.get("output_key", "result")
        max_iters = max(1, min(5, int(self.config.get("max_iterations", 3))))

        eval_llm = _get_model_spec(model_id).build_llm()

        draft = ""
        feedback: str | None = None
        passed = False
        attempt = 0
        for attempt in range(1, max_iters + 1):
            if feedback:
                gen_prompt = (
                    f"{base_prompt}\n\n"
                    f"Your previous attempt:\n{draft}\n\n"
                    f"A reviewer judged it insufficient:\n{feedback}\n\n"
                    "Produce an improved version that fully addresses the critique."
                )
                _emit(task_state, "node_token", node_id=self.node_id,
                      text=f"\n\n— revising (attempt {attempt}/{max_iters}) —\n\n")
            else:
                gen_prompt = base_prompt

            draft = await _run_agent_text(self.node_id, model_id, gen_prompt, task_state)

            passed, feedback = await _evaluate_draft(eval_llm, rubric, base_prompt, draft)
            _emit(task_state, "node_token", node_id=self.node_id,
                  text=f"\n\n— reviewer: {'PASS' if passed else 'FAIL'} —\n\n")
            if passed:
                break

        return NodeOutput(data={
            output_key: draft,
            f"{output_key}_iterations": attempt,
            f"{output_key}_passed": passed,
        })


# ── SequentialNode (ADK SequentialAgent analog) ──────────────────────────

class SequentialNode(BaseNode):
    """ADK SequentialAgent analog — runs agent steps in order, sharing state.

    Each step's output is injected into the next step's template via
    ``{{output_key}}``. All steps run on the same inputs plus accumulated
    outputs from prior steps. Tokens stream as ``node_token`` with a
    sub-step label.

    Config fields:
        steps (list[dict]): each item ``{"prompt_template": str,
            "output_key": str (default step_<idx>), "model": str (optional),
            "label": str (optional)}``
        output_key (str): if set, only this key is returned as final output
            aggregated (otherwise all step outputs + inputs are returned).

    Example:
        steps=[
          {"prompt_template": "Research {{topic}}", "output_key": "research"},
          {"prompt_template": "Write report using {{research}}",
           "output_key": "report"}
        ]
    """

    node_type = "sequential"

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        steps = self.config.get("steps", [])
        if not steps:
            raise ValueError("SequentialNode requires non-empty 'steps' list")
        model_default = self.config.get("model", DEFAULT_MODEL) or DEFAULT_MODEL
        final_output_key = self.config.get("output_key")

        data: dict[str, Any] = dict(inputs)
        for idx, step in enumerate(steps):
            template = step.get("prompt_template", "")
            output_key = step.get("output_key", f"step_{idx}")
            model_id = step.get("model", model_default) or model_default
            label = step.get("label", output_key)
            output_schema = step.get("output_schema")

            prompt = _interpolate(template, data)
            if output_schema:
                schema_str = output_schema if isinstance(output_schema, str) else json.dumps(output_schema, indent=2)
                prompt = f"{prompt}\n\nYou must output valid JSON matching:\n{schema_str}\nOutput ONLY JSON."
            _emit(
                task_state,
                "node_token",
                node_id=self.node_id,
                text=f"\n\n— sequential step {idx + 1}/{len(steps)}: {label} —\n\n",
            )
            result = await _run_agent_text(f"{self.node_id}_seq_{idx}", model_id, prompt, task_state)
            if output_schema:
                parsed, _ = _maybe_parse_structured_output(result, output_schema)
                data[output_key] = parsed if isinstance(parsed, (dict, list)) else result
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        if k not in data:
                            data[k] = v
            else:
                data[output_key] = result

        # Final structured output if requested at node level
        final_output_schema = self.config.get("output_schema")
        if final_output_key:
            val = data.get(final_output_key, "")
            if final_output_schema and isinstance(val, str):
                parsed, _ = _maybe_parse_structured_output(val, final_output_schema)
                if isinstance(parsed, (dict, list)):
                    return NodeOutput(data={final_output_key: parsed, f"{final_output_key}_raw": val})
            return NodeOutput(data={final_output_key: val})
        if final_output_schema:
            # Try to parse entire data dict as JSON? If output_key not set, look for "result"
            maybe_text = data.get("result") or json.dumps(data)
            if isinstance(maybe_text, str):
                parsed, _ = _maybe_parse_structured_output(maybe_text, final_output_schema)
                if isinstance(parsed, (dict, list)):
                    return NodeOutput(data=parsed if isinstance(parsed, dict) else {"result": parsed})
        return NodeOutput(data=data)


# ── ParallelNode (ADK ParallelAgent analog) ─────────────────────────────────

class ParallelNode(BaseNode):
    """ADK ParallelAgent analog — runs agent branches concurrently.

    Each branch interpolates its prompt from the *same* input dict (no cross-
    branch dependency, true fan-out). Concurrency is bounded optionally.

    Config fields:
        branches (list[dict]): each ``{"prompt_template": str,
            "output_key": str (default branch_<idx>), "model": str (optional),
            "label": str (optional)}``
        concurrency (int | None): max parallel branches, None = all at once
        output_key (str): if set, return this key's merged view? Ignored for now,
            returns all branch outputs plus inputs.
    """

    node_type = "parallel"

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        branches = self.config.get("branches", [])
        if not branches:
            raise ValueError("ParallelNode requires non-empty 'branches' list")
        model_default = self.config.get("model", DEFAULT_MODEL) or DEFAULT_MODEL
        concurrency = self.config.get("concurrency")

        _emit(task_state, "map_start", node_id=self.node_id, total=len(branches))

        async def run_branch(idx: int, branch: dict) -> tuple[str, Any]:
            template = branch.get("prompt_template", "")
            output_key = branch.get("output_key", f"branch_{idx}")
            model_id = branch.get("model", model_default) or model_default
            label = branch.get("label", output_key)
            prompt = _interpolate(template, inputs)
            result = await _run_agent_text(f"{self.node_id}_par_{idx}", model_id, prompt, task_state)
            _emit(task_state, "map_item_done", node_id=self.node_id, index=idx, result={output_key: result, "label": label})
            return output_key, result

        async def _gather_with_exceptions(coros):
            # Use return_exceptions=True so one failing branch doesn't leave
            # siblings running detached with tokens still streaming.
            results = await asyncio.gather(*coros, return_exceptions=True)
            # Re-raise first real exception, but log others
            errors = [r for r in results if isinstance(r, BaseException)]
            if errors:
                # If any branch errored, surface the first; others already logged via _emit
                for err in errors:
                    if not isinstance(err, asyncio.CancelledError):
                        raise err
            return [r for r in results if not isinstance(r, BaseException)]

        if concurrency:
            sem = asyncio.Semaphore(int(concurrency))

            async def run_limited(i: int, b: dict):
                async with sem:
                    return await run_branch(i, b)

            gathered = await _gather_with_exceptions([run_limited(i, b) for i, b in enumerate(branches)])
        else:
            gathered = await _gather_with_exceptions([run_branch(i, b) for i, b in enumerate(branches)])

        data: dict[str, Any] = dict(inputs)
        for key, value in gathered:
            data[key] = value

        return NodeOutput(data=data)


# ── LoopNode (ADK LoopAgent analog) ─────────────────────────────────────────

class LoopNode(BaseNode):
    """ADK LoopAgent analog — loops generate -> evaluate until PASS or max_iter.

    Generalizes RefineNode with configurable exit and clearer events.

    Config fields:
        prompt_template (str): generation task with {{var}} placeholders
        rubric (str): criteria evaluator judges against; if empty, runs
            max_iterations unconditionally (no early exit)
        model (str): model id for generation + evaluation (default DEFAULT_MODEL)
        max_iterations (int): max rounds (default 3, clamped 1..10)
        output_key (str): output port name (default "result")
        exit_on (str): token to indicate done (default "PASS"); evaluator must
            start with this token to signal success. Set to "" to disable
            early exit.
    """

    node_type = "loop"

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        base_prompt = _interpolate(self.config.get("prompt_template", ""), inputs)
        rubric = _interpolate(self.config.get("rubric", ""), inputs)
        model_id = self.config.get("model", DEFAULT_MODEL) or DEFAULT_MODEL
        output_key = self.config.get("output_key", "result")
        max_iters = max(1, min(10, int(self.config.get("max_iterations", 3))))
        exit_on = str(self.config.get("exit_on", "PASS")).strip().upper() or "PASS"

        eval_llm = _get_model_spec(model_id).build_llm()

        draft = ""
        feedback: str | None = None
        passed = False
        attempt = 0

        for attempt in range(1, max_iters + 1):
            if feedback:
                gen_prompt = (
                    f"{base_prompt}\n\n"
                    f"Your previous attempt:\n{draft}\n\n"
                    f"A reviewer judged it insufficient:\n{feedback}\n\n"
                    "Produce an improved version that fully addresses the critique."
                )
                _emit(
                    task_state,
                    "node_token",
                    node_id=self.node_id,
                    text=f"\n\n— loop revising (attempt {attempt}/{max_iters}) —\n\n",
                )
            else:
                gen_prompt = base_prompt
                _emit(
                    task_state,
                    "node_token",
                    node_id=self.node_id,
                    text=f"\n\n— loop start (attempt {attempt}/{max_iters}) —\n\n",
                )

            draft = await _run_agent_text(f"{self.node_id}_loop_{attempt}", model_id, gen_prompt, task_state)

            if not rubric:
                # No rubric -> no evaluation, just iterate
                _emit(
                    task_state,
                    "node_token",
                    node_id=self.node_id,
                    text=f"\n\n— loop iteration {attempt}/{max_iters} done (no rubric) —\n\n",
                )
                continue

            passed, feedback = await _evaluate_draft(eval_llm, rubric, base_prompt, draft)
            _emit(
                task_state,
                "node_token",
                node_id=self.node_id,
                text=f"\n\n— reviewer: {'PASS' if passed else 'FAIL'} (attempt {attempt}) —\n\n",
            )
            if passed:
                break

        return NodeOutput(
            data={
                output_key: draft,
                f"{output_key}_iterations": attempt,
                f"{output_key}_passed": passed if rubric else True,
            }
        )


# ── Approval / Human Input Nodes (HITL) ────────────────────────────────────

class ApprovalNode(BaseNode):
    """Human approval gate — pauses workflow until approved/denied.

    Emits approval_request and waits for resume via TaskState.resume_future
    (resolved by GraphQL resumeWorkflowRun / resolveWorkflowApproval).

    Config:
        reason (str): approval reason with {{var}} placeholders
        tool (str): optional tool/name label, defaults to node_id
        timeout_seconds (int | None): auto-fail after N seconds
        on_deny (str): "error" (default, raises) or "continue" (routes to denied)
    """

    node_type = "approval"

    def output_ports(self) -> list[str]:
        # Supports branching: approved vs denied
        return ["approved", "denied"]

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        reason_tmpl = self.config.get("reason", "Approval required to continue")
        tool = self.config.get("tool", self.node_id)
        timeout = self.config.get("timeout_seconds")
        on_deny = self.config.get("on_deny", "error")
        reason = _interpolate(reason_tmpl, inputs)

        _emit(
            task_state,
            "approval_request",
            tool=tool,
            reason=reason,
            args=json.dumps(inputs),
            node_id=self.node_id,
        )
        # Also emit interrupt for generic UI
        _emit(
            task_state,
            "interrupt",
            interrupt_id=self.node_id,
            question=f"{tool}: {reason}",
        )

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        task_state.pending_interrupt_id = self.node_id
        task_state.resume_future = fut
        try:
            if timeout:
                answer = await asyncio.wait_for(fut, timeout=float(timeout))
            else:
                # Poll for cancellation so queue stop works even if future not cancelled
                while not fut.done():
                    if task_state.cancelled:
                        raise asyncio.CancelledError()
                    await asyncio.sleep(0.2)
                answer = fut.result()
        except asyncio.TimeoutError:
            _emit(
                task_state,
                "approval_resolved",
                tool=tool,
                approved=False,
                answer="timeout",
                node_id=self.node_id,
            )
            raise RuntimeError(f"Approval timed out for {tool}: {reason}")
        finally:
            task_state.pending_interrupt_id = None
            task_state.resume_future = None

        # Parse approval
        approved = True
        answer_str = str(answer) if answer is not None else ""
        if isinstance(answer, bool):
            approved = answer
        elif isinstance(answer, dict):
            approved = bool(answer.get("approved", True))
            answer_str = answer.get("answer", answer_str) or json.dumps(answer)
        else:
            try:
                from core.approval import is_affirmative_answer

                approved = is_affirmative_answer(answer_str)
            except Exception:
                # Fallback: truthy string containing approve/yes
                low = answer_str.strip().lower()
                approved = low not in ("no", "deny", "denied", "n", "reject", "false", "0")

        _emit(
            task_state,
            "approval_resolved",
            tool=tool,
            approved=approved,
            answer=answer_str,
            node_id=self.node_id,
        )
        _emit(
            task_state,
            "interrupt_resolved",
            interrupt_id=self.node_id,
        )

        if approved:
            return NodeOutput(data={**inputs, "approved": True, "answer": answer_str}, next_handles=["approved"])
        else:
            if on_deny == "continue":
                return NodeOutput(data={**inputs, "approved": False, "answer": answer_str}, next_handles=["denied"])
            raise RuntimeError(f"Approval denied for {tool}: {reason} — answer: {answer_str}")


class HumanInputNode(BaseNode):
    """Requests free-text human input and pauses.

    Config:
        prompt (str): question/prompt with {{var}}
        output_key (str): output port name, default "answer"
        timeout_seconds (int | None)
    """

    node_type = "human_input"

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        prompt_tmpl = str(self.config.get("prompt", self.config.get("question", "Human input required")) or "Human input required")
        output_key = self.config.get("output_key", "answer")
        timeout = self.config.get("timeout_seconds")
        prompt = _interpolate(prompt_tmpl, inputs)

        _emit(
            task_state,
            "interrupt",
            interrupt_id=self.node_id,
            question=prompt,
        )
        _emit(
            task_state,
            "approval_request",
            tool=self.node_id,
            reason=prompt,
            args=json.dumps(inputs),
            node_id=self.node_id,
        )

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        task_state.pending_interrupt_id = self.node_id
        task_state.resume_future = fut
        try:
            if timeout:
                answer = await asyncio.wait_for(fut, timeout=float(timeout))
            else:
                while not fut.done():
                    if task_state.cancelled:
                        raise asyncio.CancelledError()
                    await asyncio.sleep(0.2)
                answer = fut.result()
        except asyncio.TimeoutError:
            _emit(
                task_state,
                "interrupt_resolved",
                interrupt_id=self.node_id,
            )
            raise RuntimeError(f"Human input timed out for {self.node_id}: {prompt}")
        finally:
            task_state.pending_interrupt_id = None
            task_state.resume_future = None

        answer_str = str(answer) if answer is not None else ""
        _emit(
            task_state,
            "interrupt_resolved",
            interrupt_id=self.node_id,
        )
        _emit(
            task_state,
            "approval_resolved",
            tool=self.node_id,
            approved=True,
            answer=answer_str,
            node_id=self.node_id,
        )
        return NodeOutput(data={**inputs, output_key: answer_str, "answer": answer_str})


# ── Registry ──────────────────────────────────────────────────────────────────

NODE_REGISTRY: dict[str, type[BaseNode]] = {
    "agent": AgentNode,
    "conditional": ConditionalNode,
    "map": MapNode,
    "start": StartNode,
    "router": RouterNode,
    "refine": RefineNode,
    "sequential": SequentialNode,
    "parallel": ParallelNode,
    "loop": LoopNode,
    "approval": ApprovalNode,
    "human_input": HumanInputNode,
    "planner": PlannerNode,
    "plan": PlannerNode,  # alias
}


def build_node(node_id: str, node_type: str, config: dict[str, Any]) -> BaseNode:
    """Instantiate a node by type name. Raises ValueError for unknown types."""
    cls = NODE_REGISTRY.get(node_type)
    if cls is None:
        raise ValueError(f"Unknown node type: {node_type!r}. Known types: {list(NODE_REGISTRY)}")
    return cls(node_id, config)
