"""File management tools: read, write, and list files on disk.

Paths under memory/ are transparently routed to the AsyncSqliteStore so
agent memory persists in the database rather than as loose files on disk.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedStore
from langgraph.store.base import BaseStore

_MEMORY_PREFIX = "memory/"


@tool
async def write_file(
    filepath: str,
    content: str,
    store: Annotated[BaseStore, InjectedStore()],
) -> str:
    """Write text content to a file, creating parent directories as needed.

    Paths starting with memory/ are saved to the persistent memory store.
    All other paths are written to the filesystem.
    """
    if filepath.startswith(_MEMORY_PREFIX):
        key = filepath[len(_MEMORY_PREFIX):]
        await store.aput(("memory",), key, {"content": content})
        return f"Saved to memory store: {key}"

    def _sync() -> str:
        path = pathlib.Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Written to {path}"
    return await asyncio.to_thread(_sync)


@tool
async def read_file(
    filepath: str,
    store: Annotated[BaseStore, InjectedStore()],
) -> str:
    """Read text content from a file.

    Paths starting with memory/ are read from the persistent memory store.
    All other paths are read from the filesystem.
    """
    if filepath.startswith(_MEMORY_PREFIX):
        key = filepath[len(_MEMORY_PREFIX):]
        item = await store.aget(("memory",), key)
        if item is None:
            return f"Not found in memory store: {key}"
        return item.value.get("content", "")

    def _sync() -> str:
        path = pathlib.Path(filepath)
        if not path.exists():
            return f"File not found: {filepath}"
        return path.read_text(encoding="utf-8")
    return await asyncio.to_thread(_sync)


@tool
async def list_files(directory: str) -> str:
    """List files in a directory."""
    def _sync() -> str:
        path = pathlib.Path(directory)
        if not path.exists():
            return f"Directory not found: {directory}"
        files = sorted(p for p in path.iterdir() if p.is_file())
        if not files:
            return f"No files found in {directory}"
        return "\n".join(str(f) for f in files)
    return await asyncio.to_thread(_sync)
