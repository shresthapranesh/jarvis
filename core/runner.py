"""Runner abstraction — ADK Runner analog.

ADK composes: session_service + artifact_service + memory_service + plugins
into a Runner that owns the LLM invocation lifecycle.

Jarvis equivalent before this was scattered across server/entrypoint.py
(lifespan builds checkpointer, store, queue, http client) and core/agents.py
(build_agent). This module formalizes it:

- JarvisRunner: holds infrastructure handles and exposes high-level methods:
  - build_agent(model, checkpointer?, store?) — delegates to core/agents
  - get_cache_config(model) — whether provider supports prompt caching
  - get_context_cache_config() — returns ContextCacheConfig

- Global accessor get_runner() / set_runner() for lifespan wiring.

Future backends (Postgres checkpointer, Redis queue) slot in here, similar
to ADK's BaseSessionService / BaseArtifactService interfaces.

No behavior change yet — just a seam so entrypoint doesn't directly poke
core.state globals everywhere, and cache config is centralized.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from httpx import AsyncClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

from core.config import AppConfig
from core.context_cache import ContextCacheConfig
from core.model_catalog import ModelSpec, get_model_spec
from core.queue import JobQueue

logger = logging.getLogger(__name__)


@dataclass
class RunnerConfig:
    """Configuration for the runner — mirrors ADK Runner's Tunables."""

    # Only providers that honor Anthropic-style cache_control blocks.
    # google_genai has its own implicit caching, not the ephemeral blocks we emit.
    cache_enabled_providers: set[str] = field(
        default_factory=lambda: {"bedrock", "anthropic"}
    )
    max_cache_breakpoints: int = 4
    context_cache_min_chars: int = 50
    # Whether to include tool schemas in cached prefix (they are, via LLM bind)
    cache_tool_schemas: bool = True
    # Budget defaults per kind (MAF TokenUsageTermination analog)
    budget_max_total_tokens: int = 500_000
    budget_max_llm_calls: int = 200
    budget_max_tool_calls: int = 300
    budget_max_duration_seconds: int = 1800
    # MCP
    mcp_enabled: bool = True


class JarvisRunner:
    """ADK Runner analog for Jarvis.

    Owns the process-wide infrastructure (checkpointer, store, queue, http,
    config) and provides a uniform entrypoint for agent construction and
    caching decisions.

    In ADK you do:
        runner = Runner(agent=root_agent, session_service=..., artifact_service=..., memory_service=...)
        await runner.run(...)

    Here we start with a thinner wrapper — construction is still via build_agent,
    but cache config and infra refs are centralized.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        checkpointer: AsyncSqliteSaver,
        store: AsyncSqliteStore,
        queue: JobQueue,
        http_client: AsyncClient,
        runner_config: RunnerConfig | None = None,
    ) -> None:
        self.config = config
        self.checkpointer = checkpointer
        self.store = store
        self.queue = queue
        self.http_client = http_client
        self.runner_config = runner_config or RunnerConfig()

    # ── Agent factory ─────────────────────────────────────────────────────

    def build_agent(self, model: str, checkpointer=None, store=None):
        """Delegate to core.agents.build_agent with runner's defaults."""
        from core.agents import build_agent as _build_agent

        return _build_agent(
            model=model,
            checkpointer=checkpointer or self.checkpointer,
            store=store or self.store,
        )

    # ── Cache config (ADK ContextCacheConfig analog) ──────────────────────

    def should_use_cache(self, model: str) -> bool:
        """Whether this model/provider benefits from prompt caching."""
        try:
            spec: ModelSpec = get_model_spec(model)
            return spec.provider in self.runner_config.cache_enabled_providers
        except Exception:
            # Unknown model — assume no cache (safe)
            return False

    def get_context_cache_config(self, model: str) -> ContextCacheConfig:
        """Build ContextCacheConfig for a given model."""
        enabled = self.should_use_cache(model)
        return ContextCacheConfig(
            enabled=enabled,
            max_breakpoints=self.runner_config.max_cache_breakpoints,
            min_chars_for_cache=self.runner_config.context_cache_min_chars,
        )

    def get_budget_limits(self, kind: str = "chat"):
        """Build BudgetLimits, merging runner config defaults with env overrides."""
        from core.budget import BudgetLimits

        base = {
            "chat": {
                "max_total_tokens": self.runner_config.budget_max_total_tokens,
                "max_llm_calls": self.runner_config.budget_max_llm_calls,
                "max_tool_calls": self.runner_config.budget_max_tool_calls,
                "max_duration_seconds": self.runner_config.budget_max_duration_seconds,
            },
            "automation": {
                "max_total_tokens": 300_000,
                "max_llm_calls": 100,
                "max_tool_calls": 150,
                "max_duration_seconds": 1200,
            },
            "workflow": {
                "max_total_tokens": 400_000,
                "max_llm_calls": 150,
                "max_tool_calls": 200,
                "max_duration_seconds": 1800,
            },
            "board_task": {
                "max_total_tokens": 400_000,
                "max_llm_calls": 150,
                "max_tool_calls": 200,
                "max_duration_seconds": 1800,
            },
        }
        cfg = base.get(kind, base["chat"])
        # env overrides
        env_limits = BudgetLimits.from_env()
        merged = dict(cfg)
        for k in cfg.keys():
            env_v = getattr(env_limits, k)
            if env_v is not None:
                merged[k] = env_v
        return BudgetLimits(**merged)

    # ── Introspection ─────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        return {
            "checkpoints_db": str(self.config.checkpoints_db),
            "artifacts_dir": str(self.config.artifacts_dir),
            "queue_backend": self.config.queue_backend,
            "runner_config": {
                "cache_enabled_providers": sorted(self.runner_config.cache_enabled_providers),
                "max_cache_breakpoints": self.runner_config.max_cache_breakpoints,
                "min_chars_for_cache": self.runner_config.context_cache_min_chars,
            },
        }


# ── Global accessor (set by lifespan, read elsewhere) ───────────────────────

_runner: JarvisRunner | None = None


def set_runner(runner: JarvisRunner | None) -> None:
    global _runner
    _runner = runner


def get_runner() -> JarvisRunner:
    if _runner is None:
        raise RuntimeError("runner not initialized — server lifespan has not started")
    return _runner


def get_runner_or_none() -> JarvisRunner | None:
    return _runner
