"""Typed accessors over persistent app/user state."""

from __future__ import annotations

from typing import Any

from langgraph.store.sqlite.aio import AsyncSqliteStore


class SessionService:
    """Typed wrapper over AsyncSqliteStore providing typed get/set for
    app, user, session, temp scopes.

    Usage:
        svc = SessionService(store)
        await svc.set_app("theme", "dark")
        await svc.get_user(user_id, "name")
    """

    def __init__(self, store: AsyncSqliteStore | None) -> None:
        self._store = store

    async def get_app(self, key: str, default: Any = None) -> Any:
        if not self._store:
            return default
        try:
            item = await self._store.aget(("app_state",), "state")
            return (item.value if item else {}).get(key, default)
        except Exception:
            return default

    async def set_app(self, key: str, value: Any) -> None:
        if not self._store:
            return
        try:
            existing = await self._store.aget(("app_state",), "state")
            merged = dict(existing.value) if existing else {}
            merged[key] = value
            await self._store.aput(("app_state",), "state", merged)
        except Exception:
            pass

    async def get_user(self, user_id: str, key: str, default: Any = None) -> Any:
        if not self._store:
            return default
        try:
            item = await self._store.aget(("user_state", user_id), "state")
            return (item.value if item else {}).get(key, default)
        except Exception:
            return default

    async def set_user(self, user_id: str, key: str, value: Any) -> None:
        if not self._store:
            return
        try:
            existing = await self._store.aget(("user_state", user_id), "state")
            merged = dict(existing.value) if existing else {}
            merged[key] = value
            await self._store.aput(("user_state", user_id), "state", merged)
        except Exception:
            pass

    async def get_all_app(self) -> dict[str, Any]:
        if not self._store:
            return {}
        try:
            item = await self._store.aget(("app_state",), "state")
            return dict(item.value) if item else {}
        except Exception:
            return {}

    async def get_all_user(self, user_id: str) -> dict[str, Any]:
        if not self._store:
            return {}
        try:
            item = await self._store.aget(("user_state", user_id), "state")
            return dict(item.value) if item else {}
        except Exception:
            return {}
