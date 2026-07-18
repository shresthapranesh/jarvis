"""InvocationContext — ADK-Go InvocationContext analog."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

TaskKind = Literal["chat", "automation", "workflow", "board_task"]


class InvocationState:
    """ADK session.State analog with prefix scoping.

    ADK-Go behavior:
      app:  - app-scoped, shared across all users/sessions, persisted in session service
      user: - per-user, persists across sessions
      temp: - ephemeral, lives only for this invocation (not persisted)
      (no prefix) - session-scoped (conversation/run)

    Jarvis mapping:
      app:  -> LangGraph store namespace ("app_state",) key="state"
      user: -> LangGraph store namespace ("user_state", user_id) key="state"
      temp: -> in-memory dict, dropped after invocation
      session -> checkpointer / TaskState + delta for Event.Actions.StateDelta
    """

    def __init__(
        self,
        initial: dict[str, Any] | None = None,
        *,
        app_state: dict[str, Any] | None = None,
        user_state: dict[str, Any] | None = None,
    ) -> None:
        self._session: dict[str, Any] = dict(initial or {})
        self._app: dict[str, Any] = dict(app_state or {})
        self._user: dict[str, Any] = dict(user_state or {})
        self._temp: dict[str, Any] = {}
        self._delta: dict[str, Any] = {}
        self._loaded = False

    def mark_loaded(self, app_state: dict | None = None, user_state: dict | None = None) -> None:
        if app_state:
            self._app.update(app_state)
        if user_state:
            self._user.update(user_state)
        self._loaded = True

    def get(self, key: str, default: Any = None) -> Any:
        if key.startswith("app:"):
            return self._app.get(key[4:], default)
        if key.startswith("user:"):
            return self._user.get(key[5:], default)
        if key.startswith("temp:"):
            return self._temp.get(key[5:], default)
        return self._session.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._delta[key] = value
        if key.startswith("app:"):
            self._app[key[4:]] = value
        elif key.startswith("user:"):
            self._user[key[4:]] = value
        elif key.startswith("temp:"):
            self._temp[key[5:]] = value
        else:
            self._session[key] = value

    def all(self, prefix: str | None = None) -> dict[str, Any]:
        if prefix == "app":
            return dict(self._app)
        if prefix == "user":
            return dict(self._user)
        if prefix == "temp":
            return dict(self._temp)
        if prefix == "session":
            return dict(self._session)
        if prefix is None:
            return {**self._app, **self._user, **self._session, **self._temp}
        return dict(self._session)

    @property
    def delta(self) -> dict[str, Any]:
        return dict(self._delta)

    def app_delta(self) -> dict[str, Any]:
        return {k[4:]: v for k, v in self._delta.items() if k.startswith("app:")}

    def user_delta(self) -> dict[str, Any]:
        return {k[5:]: v for k, v in self._delta.items() if k.startswith("user:")}

    def session_delta(self) -> dict[str, Any]:
        return {k: v for k, v in self._delta.items() if not k.startswith(("app:", "user:", "temp:"))}


@dataclass
class InvocationActions:
    state_delta: dict[str, Any] = field(default_factory=dict)
    artifact_delta: dict[str, int] = field(default_factory=dict)
    transfer_to_agent: str | None = None
    escalate: bool = False
    skip_summarization: bool = False
    requested_tool_confirmations: list[str] = field(default_factory=list)


@dataclass
class RunConfig:
    model: str | None = None
    max_tokens: int | None = None
    streaming: bool = True
    save_input_blobs_as_artifacts: bool = False


@dataclass
class InvocationContext:
    """Per-request context — replaces implicit core.state globals."""

    invocation_id: str = field(default_factory=lambda: f"inv_{uuid.uuid4().hex[:12]}")
    session_id: str = ""
    user_id: str = "default"
    branch: str | None = None
    kind: TaskKind = "chat"
    state: InvocationState = field(default_factory=InvocationState)
    run_config: RunConfig = field(default_factory=RunConfig)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    checkpointer: Any | None = field(default=None, repr=False)
    store: Any | None = field(default=None, repr=False)
    queue: Any | None = field(default=None, repr=False)
    http_client: Any | None = field(default=None, repr=False)

    def build_actions(self) -> InvocationActions:
        return InvocationActions(state_delta=self.state.delta)

    async def load_persisted_state(self) -> None:
        """Load app: and user: state from LangGraph store (ADK session service behavior).

        Called automatically by runner factory if store present, or manually.
        """
        if self.store is None or self.state._loaded:
            return
        try:
            app_item = await self.store.aget(("app_state",), "state")
            app_data = app_item.value if app_item else {}
        except Exception:
            app_data = {}
        try:
            user_item = await self.store.aget(("user_state", self.user_id), "state")
            user_data = user_item.value if user_item else {}
        except Exception:
            user_data = {}
        self.state.mark_loaded(app_state=app_data, user_state=user_data)

    async def persist_state_deltas(self) -> None:
        """Persist app: and user: deltas to store (ADK Event.Actions.StateDelta commit).

        Call at end of invocation. temp: and session-scoped are intentionally NOT persisted.
        """
        if self.store is None:
            return
        app_d = self.state.app_delta()
        if app_d:
            try:
                existing = await self.store.aget(("app_state",), "state")
                merged = dict(existing.value) if existing else {}
                merged.update(app_d)
                await self.store.aput(("app_state",), "state", merged)
            except Exception:
                pass
        user_d = self.state.user_delta()
        if user_d:
            try:
                existing = await self.store.aget(("user_state", self.user_id), "state")
                merged = dict(existing.value) if existing else {}
                merged.update(user_d)
                await self.store.aput(("user_state", self.user_id), "state", merged)
            except Exception:
                pass

    @classmethod
    def for_session(
        cls,
        session_id: str,
        user_id: str = "default",
        kind: TaskKind = "chat",
        model: str | None = None,
        initial_state: dict[str, Any] | None = None,
    ) -> "InvocationContext":
        return cls(
            session_id=session_id,
            user_id=user_id,
            kind=kind,
            state=InvocationState(initial=initial_state),
            run_config=RunConfig(model=model),
        )
