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
from core.model_catalog import AVAILABLE_MODELS, DEFAULT_MODEL
from core.state import TaskState, _notify
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
    spec = next((m for m in AVAILABLE_MODELS if m.id == model_id), None)
    if spec is None:
        spec = next(m for m in AVAILABLE_MODELS if m.id == DEFAULT_MODEL)
    return spec


def _emit(task_state: TaskState, event: str, **data: Any) -> None:
    task_state.events.append({"event": event, "data": json.dumps(data)})
    _notify(task_state)


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
        from core.state import get_async_checkpointer, get_store

        prompt = _interpolate(self.config.get("prompt_template", ""), inputs)
        model_id = self.config.get("model", DEFAULT_MODEL)
        output_key = self.config.get("output_key", "result")

        agent = build_agent(
            model=model_id,
            checkpointer=get_async_checkpointer(),
            store=get_store(),
        )
        thread_id = str(uuid4())
        run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
        stream_input: Any = {"messages": [{"role": "user", "content": prompt}]}

        final_text = ""
        async for raw_chunk in agent.astream(
            stream_input,
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
                _emit(task_state, "node_token", node_id=self.node_id, text=text)

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


# ── Registry ──────────────────────────────────────────────────────────────────

NODE_REGISTRY: dict[str, type[BaseNode]] = {
    "agent": AgentNode,
    "conditional": ConditionalNode,
    "map": MapNode,
    "start": StartNode,
}


def build_node(node_id: str, node_type: str, config: dict[str, Any]) -> BaseNode:
    """Instantiate a node by type name. Raises ValueError for unknown types."""
    cls = NODE_REGISTRY.get(node_type)
    if cls is None:
        raise ValueError(f"Unknown node type: {node_type!r}. Known types: {list(NODE_REGISTRY)}")
    return cls(node_id, config)
