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

def _interpolate(template: str, inputs: dict[str, Any]) -> str:
    """Replace ``{{var}}`` placeholders in template with values from inputs."""
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
    from core.log_callback import AgentLogger
    from core.state import get_async_checkpointer, get_store
    from langchain_core.runnables import RunnableConfig

    agent = build_agent(
        model=model_id,
        checkpointer=get_async_checkpointer(),
        store=get_store(),
    )
    run_config: RunnableConfig = {
        "configurable": {"thread_id": str(uuid4())},
        "recursion_limit": 100,
        "callbacks": [AgentLogger()],
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


# ── AgentNode ─────────────────────────────────────────────────────────────────

class AgentNode(BaseNode):
    """Runs the full agentic loop (web search, code execution, etc.) with a
    prompt template rendered from its inputs.

    Config fields:
        prompt_template (str):  template with ``{{var}}`` placeholders
        model (str):            model id, defaults to DEFAULT_MODEL
        output_key (str):       name of the output port, defaults to "result"
        input_ports (list[str]): expected input port names
    """

    node_type = "agent"

    async def execute(self, inputs: dict[str, Any], task_state: TaskState) -> NodeOutput:
        prompt = _interpolate(self.config.get("prompt_template", ""), inputs)
        model_id = self.config.get("model", DEFAULT_MODEL)
        output_key = self.config.get("output_key", "result")

        final_text = await _run_agent_text(self.node_id, model_id, prompt, task_state)
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


# ── Registry ──────────────────────────────────────────────────────────────────

NODE_REGISTRY: dict[str, type[BaseNode]] = {
    "agent": AgentNode,
    "conditional": ConditionalNode,
    "map": MapNode,
    "start": StartNode,
    "router": RouterNode,
    "refine": RefineNode,
}


def build_node(node_id: str, node_type: str, config: dict[str, Any]) -> BaseNode:
    """Instantiate a node by type name. Raises ValueError for unknown types."""
    cls = NODE_REGISTRY.get(node_type)
    if cls is None:
        raise ValueError(f"Unknown node type: {node_type!r}. Known types: {list(NODE_REGISTRY)}")
    return cls(node_id, config)
