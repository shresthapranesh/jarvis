"""Stateful per-session IPython kernels — a Jupyter-notebook-like coding session.

Backs the `run_cell` tool (`tools/code.py`). Unlike `execute` (a fresh
subprocess per call — `tools/execute.py` wrapped by `core/safety.py`), each
session gets ONE long-lived IPython kernel, so variables, imports, and
in-memory data persist across cells the way a notebook does.

A "session" is keyed by conversation_id (or, for CLI/automation/workflow runs
that have no conversation, the LangGraph thread_id). One kernel per key.

Lifecycle:
  - lazily started on the first cell for a key
  - serialized per session (one cell at a time) via an ``asyncio.Lock`` —
    concurrent ``execute`` requests on a single kernel would interleave
  - idle-reaped by a scheduler job (``core/scheduler.register_kernel_reaper_job``)
  - LRU-evicted once more than ``MAX_KERNELS`` are live
  - shut down on conversation delete (``db/ops.delete_conversation``) and on
    server shutdown (``server/entrypoint`` lifespan)

The kernel is launched from the current interpreter (``sys.executable -m
ipykernel_launcher``) so it sees exactly the packages in this venv (httpx,
pandas, yfinance, …), and over IPC transport so nothing crosses TCP in plain
text.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time

from jupyter_client.manager import AsyncKernelManager

logger = logging.getLogger(__name__)

# Strip the ANSI color codes IPython wraps tracebacks in — the LLM reads plain text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Tunables. Kept module-level (not config rows) for now; promote to AppConfig
# if these need per-deployment overrides.
MAX_KERNELS = 12               # hard cap on concurrent live kernels (LRU-evict beyond)
IDLE_TIMEOUT_SECONDS = 30 * 60  # reap a kernel untouched for this long
DEFAULT_CELL_TIMEOUT = 60      # per-cell wall-clock limit (mirrors execute())
STARTUP_TIMEOUT = 60           # kernel boot budget
INTERRUPT_DRAIN_TIMEOUT = 5    # after interrupting, how long to wait for the kernel to settle
MAX_OUTPUT_CHARS = 30_000      # cap a single cell's captured output


class KernelSession:
    """One long-lived IPython kernel, serialized by an ``asyncio.Lock``."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.km: AsyncKernelManager | None = None
        self.kc = None
        self.lock = asyncio.Lock()
        self.last_used = time.monotonic()

    async def _ensure_started(self) -> None:
        """Start the kernel if it isn't running (also restarts a dead one)."""
        if self.km is not None and await self.km.is_alive():
            return
        if self.km is not None:
            # Was started but died — tear the stale client/manager down first.
            await self._teardown()
        km = AsyncKernelManager()
        km.transport = "ipc"  # unix-socket endpoints; no plaintext TCP, no warning
        # Launch from THIS interpreter so the kernel sees the venv's packages.
        # kernel_cmd is a valid (non-deprecated) traitlets attr here; just absent
        # from the type stubs.
        km.kernel_cmd = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]  # type: ignore[missing-attribute]
        await km.start_kernel()
        kc = km.client()
        kc.start_channels()
        try:
            await kc.wait_for_ready(timeout=STARTUP_TIMEOUT)
        except RuntimeError:
            kc.stop_channels()
            await km.shutdown_kernel(now=True)
            raise
        self.km, self.kc = km, kc
        logger.info("kernel started for session %s", self.key)

    async def _drain_until_idle(self, msg_id: str, out: list[str]) -> None:
        """Read iopub until the ``idle`` status for *our* execute request.

        Filtering by parent ``msg_id`` ignores trailing messages from a
        previously-interrupted cell, so the channel is always left clean for
        the next cell.
        """
        kc = self.kc
        assert kc is not None
        while True:
            msg = await kc.get_iopub_msg()
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            mtype = msg["header"]["msg_type"]
            content = msg["content"]
            if mtype == "stream":
                out.append(content.get("text", ""))
            elif mtype == "execute_result":
                out.append(content.get("data", {}).get("text/plain", ""))
            elif mtype == "display_data":
                data = content.get("data", {})
                if "text/plain" in data:
                    out.append(data["text/plain"])
                # Non-text rich output (images, HTML) can't ride the text
                # channel — note it so the agent knows something was produced.
                rich = [m for m in data if m != "text/plain"]
                if rich:
                    out.append(f"[{', '.join(sorted(rich))} output — not shown as text]")
            elif mtype == "error":
                out.append(_ANSI.sub("", "\n".join(content.get("traceback", []))))
            elif mtype == "status" and content.get("execution_state") == "idle":
                return

    async def run(self, code: str, timeout: float = DEFAULT_CELL_TIMEOUT) -> str:
        """Execute one cell, returning combined stdout/stderr/result/traceback text.

        Serialized via ``self.lock``. On timeout the kernel is interrupted
        (not killed) so the session's variables survive, then we drain the
        interrupted cell's trailing messages before returning.
        """
        async with self.lock:
            await self._ensure_started()
            self.last_used = time.monotonic()
            kc, km = self.kc, self.km
            assert kc is not None and km is not None
            msg_id = kc.execute(code)
            out: list[str] = []
            interrupted = False
            try:
                await asyncio.wait_for(self._drain_until_idle(msg_id, out), timeout)
            except asyncio.TimeoutError:
                interrupted = True
                await km.interrupt_kernel()
                try:
                    await asyncio.wait_for(
                        self._drain_until_idle(msg_id, out), INTERRUPT_DRAIN_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                # The agent run was cancelled mid-cell — interrupt so the
                # kernel doesn't keep churning, then propagate.
                await km.interrupt_kernel()
                raise
            finally:
                self.last_used = time.monotonic()

            result = "".join(out).strip()
            if len(result) > MAX_OUTPUT_CHARS:
                result = result[:MAX_OUTPUT_CHARS] + f"\n... [truncated {len(result) - MAX_OUTPUT_CHARS} chars]"
            if interrupted:
                note = f"[execution timed out after {int(timeout)}s — kernel interrupted; session state is preserved]"
                result = (result + "\n" + note).strip() if result else note
            return result or "(no output)"

    async def _teardown(self) -> None:
        if self.kc is not None:
            try:
                self.kc.stop_channels()
            except Exception:
                pass
            self.kc = None
        if self.km is not None:
            try:
                await self.km.shutdown_kernel(now=True)
            except Exception as exc:
                logger.debug("kernel shutdown error for %s: %s", self.key, exc)
            self.km = None

    async def shutdown(self) -> None:
        async with self.lock:
            await self._teardown()
        logger.info("kernel shut down for session %s", self.key)


class KernelRegistry:
    """Process-wide registry of per-session kernels with an LRU cap."""

    def __init__(self) -> None:
        self._sessions: dict[str, KernelSession] = {}
        self._lock = asyncio.Lock()  # guards create / evict bookkeeping only

    async def _get_or_create(self, key: str) -> KernelSession:
        victim: KernelSession | None = None
        async with self._lock:
            session = self._sessions.get(key)
            if session is not None:
                return session
            if len(self._sessions) >= MAX_KERNELS:
                victim_key = min(self._sessions, key=lambda k: self._sessions[k].last_used)
                victim = self._sessions.pop(victim_key)
                logger.info("evicting LRU kernel %s (capacity %d)", victim_key, MAX_KERNELS)
            session = KernelSession(key)
            self._sessions[key] = session
        # Tear the evicted kernel down OUTSIDE the registry lock — victim.shutdown()
        # waits on the victim's own session lock (it may be mid-cell), which must
        # not stall get-or-create for every other session.
        if victim is not None:
            await victim.shutdown()
        return session

    async def run_cell(self, key: str, code: str, timeout: float = DEFAULT_CELL_TIMEOUT) -> str:
        session = await self._get_or_create(key)
        return await session.run(code, timeout=timeout)

    async def shutdown(self, key: str) -> None:
        async with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            await session.shutdown()

    async def shutdown_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.shutdown()

    async def reap_idle(self, max_idle: float = IDLE_TIMEOUT_SECONDS) -> int:
        """Shut down kernels untouched for longer than ``max_idle`` seconds."""
        now = time.monotonic()
        async with self._lock:
            stale = [k for k, s in self._sessions.items() if now - s.last_used > max_idle]
            sessions = [self._sessions.pop(k) for k in stale]
        for session in sessions:
            await session.shutdown()
        if stale:
            logger.info("reaped %d idle kernel(s): %s", len(stale), ", ".join(stale))
        return len(stale)


_registry: KernelRegistry | None = None


def get_kernel_registry() -> KernelRegistry:
    global _registry
    if _registry is None:
        _registry = KernelRegistry()
    return _registry
