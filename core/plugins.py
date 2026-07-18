"""Plugin system — ADK-Go plugin.Config 12-hooks analog.

ADK-Go:
  type Config struct {
    Name string
    OnUserMessage func(...)
    OnEvent func(...)
    BeforeRun func(...)
    AfterRun func(...)
    BeforeAgent func(...)
    AfterAgent func(...)
    BeforeModel func(...)
    AfterModel func(...)
    OnModelError func(...)
    BeforeTool func(...)
    AfterTool func(...)
    OnToolError func(...)
  }

Jarvis Python version:
  Uses Protocol + dataclass for plugins, plus LangChain BaseCallbackHandler bridge
  so existing AgentLogger, BudgetCallbackHandler, UsageAccumulator can be plugins
  without rewriting.

  Runner owns plugins list; each runtime calls plugin_manager hooks at right place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.invocation_context import InvocationContext

logger = logging.getLogger(__name__)


@runtime_checkable
class Plugin(Protocol):
    name: str

    async def on_user_message(self, ctx: InvocationContext, message: str) -> None: ...
    async def on_event(self, ctx: InvocationContext, event: dict) -> None: ...
    async def before_run(self, ctx: InvocationContext) -> None: ...
    async def after_run(self, ctx: InvocationContext, result: Any | None = None) -> None: ...
    async def before_agent(self, ctx: InvocationContext, agent: Any | None = None) -> None: ...
    async def after_agent(self, ctx: InvocationContext, result: Any | None = None) -> None: ...
    async def before_model(self, ctx: InvocationContext, request: Any) -> Any | None: ...
    async def after_model(self, ctx: InvocationContext, response: Any) -> None: ...
    async def on_model_error(self, ctx: InvocationContext, error: BaseException) -> None: ...
    async def before_tool(self, ctx: InvocationContext, tool_call: dict) -> dict | None: ...
    async def after_tool(self, ctx: InvocationContext, tool_call: dict, result: Any) -> None: ...
    async def on_tool_error(self, ctx: InvocationContext, tool_call: dict, error: BaseException) -> None: ...


@dataclass
class BasePlugin:
    """Base class implementing no-op hooks so subclasses override only what they need."""

    name: str = "base"

    async def on_user_message(self, ctx: InvocationContext, message: str) -> None:
        pass

    async def on_event(self, ctx: InvocationContext, event: dict) -> None:
        pass

    async def before_run(self, ctx: InvocationContext) -> None:
        pass

    async def after_run(self, ctx: InvocationContext, result: Any | None = None) -> None:
        pass

    async def before_agent(self, ctx: InvocationContext, agent: Any | None = None) -> None:
        pass

    async def after_agent(self, ctx: InvocationContext, result: Any | None = None) -> None:
        pass

    async def before_model(self, ctx: InvocationContext, request: Any) -> Any | None:
        return None

    async def after_model(self, ctx: InvocationContext, response: Any) -> None:
        pass

    async def on_model_error(self, ctx: InvocationContext, error: BaseException) -> None:
        pass

    async def before_tool(self, ctx: InvocationContext, tool_call: dict) -> dict | None:
        return None

    async def after_tool(self, ctx: InvocationContext, tool_call: dict, result: Any) -> None:
        pass

    async def on_tool_error(self, ctx: InvocationContext, tool_call: dict, error: BaseException) -> None:
        pass

    def as_callback_handler(self) -> Any | None:
        return None


class LoggingPlugin(BasePlugin):
    """ADK loggingplugin analog — logs via AgentLogger bridge."""

    name: str = "logging"

    def __init__(self) -> None:
        super().__init__(name="logging")
        self._logger_handler: Any | None = None

    def as_callback_handler(self) -> Any | None:
        if self._logger_handler is None:
            try:
                from core.log_callback import AgentLogger

                self._logger_handler = AgentLogger()
            except Exception:
                return None
        return self._logger_handler

    async def before_run(self, ctx: InvocationContext) -> None:
        logger.debug("plugin[logging] before_run inv=%s sess=%s kind=%s", ctx.invocation_id, ctx.session_id, ctx.kind)

    async def after_run(self, ctx: InvocationContext, result: Any | None = None) -> None:
        logger.debug("plugin[logging] after_run inv=%s delta=%s", ctx.invocation_id, ctx.state.delta)


class BudgetPlugin(BasePlugin):
    name: str = "budget"

    def __init__(self, tracker: Any | None = None, task_state: Any | None = None) -> None:
        super().__init__(name="budget")
        self._tracker = tracker
        self._task_state = task_state
        self._handler: Any | None = None

    def as_callback_handler(self) -> Any | None:
        if self._handler is None and self._tracker is not None:
            try:
                from core.budget import BudgetCallbackHandler

                self._handler = BudgetCallbackHandler(self._tracker, task_state=self._task_state)
            except Exception:
                return None
        return self._handler


class UsagePlugin(BasePlugin):
    name: str = "usage"

    def __init__(self) -> None:
        super().__init__(name="usage")
        self._handler: Any | None = None

    def as_callback_handler(self) -> Any | None:
        if self._handler is None:
            try:
                from core.log_callback import UsageAccumulator

                self._handler = UsageAccumulator()
            except Exception:
                return None
        return self._handler

    @property
    def input_tokens(self) -> int:
        return getattr(self._handler, "input_tokens", 0) if self._handler else 0

    @property
    def output_tokens(self) -> int:
        return getattr(self._handler, "output_tokens", 0) if self._handler else 0

    @property
    def has_usage(self) -> bool:
        return getattr(self._handler, "has_usage", False) if self._handler else False


@dataclass
class PluginManager:
    """Owns list of plugins, mirrors ADK runner's plugin chaining."""

    plugins: list[BasePlugin] = field(default_factory=list)

    def add(self, plugin: BasePlugin) -> None:
        self.plugins.append(plugin)

    def get_callback_handlers(self) -> list[Any]:
        handlers = []
        for p in self.plugins:
            try:
                h = p.as_callback_handler()
                if h is not None:
                    handlers.append(h)
            except Exception:
                continue
        return handlers

    async def dispatch_before_run(self, ctx: InvocationContext) -> None:
        for p in self.plugins:
            try:
                await p.before_run(ctx)
            except Exception as exc:
                logger.warning("plugin[%s] before_run failed: %s", p.name, exc)

    async def dispatch_after_run(self, ctx: InvocationContext, result: Any | None = None) -> None:
        for p in self.plugins:
            try:
                await p.after_run(ctx, result)
            except Exception as exc:
                logger.warning("plugin[%s] after_run failed: %s", p.name, exc)

    async def dispatch_before_agent(self, ctx: InvocationContext, agent: Any | None = None) -> None:
        for p in self.plugins:
            try:
                await p.before_agent(ctx, agent)
            except Exception as exc:
                logger.warning("plugin[%s] before_agent failed: %s", p.name, exc)

    async def dispatch_after_agent(self, ctx: InvocationContext, result: Any | None = None) -> None:
        for p in self.plugins:
            try:
                await p.after_agent(ctx, result)
            except Exception as exc:
                logger.warning("plugin[%s] after_agent failed: %s", p.name, exc)

    async def dispatch_before_tool(self, ctx: InvocationContext, tool_call: dict) -> dict | None:
        result = None
        for p in self.plugins:
            try:
                r = await p.before_tool(ctx, tool_call)
                if r is not None:
                    result = r
            except Exception as exc:
                logger.warning("plugin[%s] before_tool failed: %s", p.name, exc)
        return result

    async def dispatch_after_tool(self, ctx: InvocationContext, tool_call: dict, tool_result: Any) -> None:
        for p in self.plugins:
            try:
                await p.after_tool(ctx, tool_call, tool_result)
            except Exception as exc:
                logger.warning("plugin[%s] after_tool failed: %s", p.name, exc)

    async def dispatch_on_tool_error(self, ctx: InvocationContext, tool_call: dict, error: BaseException) -> None:
        for p in self.plugins:
            try:
                await p.on_tool_error(ctx, tool_call, error)
            except Exception as exc:
                logger.warning("plugin[%s] on_tool_error failed: %s", p.name, exc)

    async def dispatch_before_model(self, ctx: InvocationContext, request: Any) -> Any | None:
        result = None
        for p in self.plugins:
            try:
                r = await p.before_model(ctx, request)
                if r is not None:
                    result = r
            except Exception as exc:
                logger.warning("plugin[%s] before_model failed: %s", p.name, exc)
        return result

    async def dispatch_after_model(self, ctx: InvocationContext, response: Any) -> None:
        for p in self.plugins:
            try:
                await p.after_model(ctx, response)
            except Exception as exc:
                logger.warning("plugin[%s] after_model failed: %s", p.name, exc)

    async def dispatch_on_model_error(self, ctx: InvocationContext, error: BaseException) -> None:
        for p in self.plugins:
            try:
                await p.on_model_error(ctx, error)
            except Exception as exc:
                logger.warning("plugin[%s] on_model_error failed: %s", p.name, exc)
