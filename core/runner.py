"""Jarvis runner — owns checkpointer, store, queue, and plugin manager."""

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
    """Runner tunables."""

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
    # Plugins (Jarvis plugin.Config analog) - list of plugin names to enable by default
    # Actual instances are built in JarvisRunner
    enable_logging_plugin: bool = True
    enable_telemetry_plugin: bool = False


class JarvisRunner:
    """Jarvis runner analog for Jarvis.

    Owns the process-wide infrastructure (checkpointer, store, queue, http,
    config) and provides a uniform entrypoint for agent construction and
    caching decisions.

    In Jarvis you do:
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
        self._plugin_manager: Any | None = None

    # ── InvocationContext factory (invocation context) ───────

    def new_invocation_context(
        self,
        session_id: str,
        user_id: str = "default",
        kind: str = "chat",
        model: str | None = None,
        branch: str | None = None,
        initial_state: dict | None = None,
    ):
        from core.invocation_context import InvocationContext, InvocationState, RunConfig

        return InvocationContext(
            session_id=session_id,
            user_id=user_id,
            kind=kind,  # type: ignore
            branch=branch,
            state=InvocationState(initial=initial_state),
            run_config=RunConfig(model=model),
            checkpointer=self.checkpointer,
            store=self.store,
            queue=self.queue,
            http_client=self.http_client,
        )

    async def new_invocation_context_async(
        self,
        session_id: str,
        user_id: str = "default",
        kind: str = "chat",
        model: str | None = None,
        branch: str | None = None,
        initial_state: dict | None = None,
    ):
        ctx = self.new_invocation_context(
            session_id=session_id,
            user_id=user_id,
            kind=kind,
            model=model,
            branch=branch,
            initial_state=initial_state,
        )
        try:
            await ctx.load_persisted_state()
        except Exception:
            pass
        return ctx

    def get_session_service(self):
        from core.session_service import SessionService

        return SessionService(self.store)

    def get_plugin_manager(self, tracker: Any | None = None, task_state: Any | None = None):
        from core.plugins import PluginManager, LoggingPlugin, BudgetPlugin, UsagePlugin

        pm = PluginManager()
        if self.runner_config.enable_logging_plugin:
            pm.add(LoggingPlugin())
        # Usage always useful
        pm.add(UsagePlugin())
        if tracker is not None:
            pm.add(BudgetPlugin(tracker=tracker, task_state=task_state))
        return pm

    def get_default_callbacks(self, tracker: Any | None = None, task_state: Any | None = None):
        """Back-compat: return list of callback handlers (AgentLogger, Usage, Budget)."""
        pm = self.get_plugin_manager(tracker=tracker, task_state=task_state)
        return pm.get_callback_handlers()

    # ── Agent factory ─────────────────────────────────────────────────────

    def build_agent(self, model: str, checkpointer=None, store=None, invocation_context=None):
        """Delegate to core.agents.build_agent with runner's defaults."""
        from core.agents import build_agent as _build_agent

        if invocation_context is not None:
            checkpointer = getattr(invocation_context, 'checkpointer', None) or checkpointer
            store = getattr(invocation_context, 'store', None) or store
        return _build_agent(
            model=model,
            checkpointer=checkpointer or self.checkpointer,
            store=store or self.store,
        )

    # ── Cache config (ContextCacheConfig analog) ──────────────────────

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
                "max_total_tokens": 600_000,
                "max_llm_calls": 200,
                "max_tool_calls": 300,
                "max_duration_seconds": 1800,
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
