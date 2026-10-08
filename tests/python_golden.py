"""Python's answers, recorded once, for the tests that diff the Rust server.

These tests used to run every scenario on both servers and compare. The
Python server is gone, so what it answered was recorded while it still
existed (`tests/golden/python/<module>.json`) and each test compares the Rust
server against that.

`await recorded(compute)` returns the value stored for this call — the n-th
`recorded` call of the running test — or, with `JARVIS_RECORD_PYTHON=1`, runs
`compute` (an async or plain callable producing Python's side) and stores
what it returns. Values keep their Python types across the round trip:
tuples, bytes, datetimes and sets are tagged in the JSON.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "python"
RECORD = os.environ.get("JARVIS_RECORD_PYTHON") == "1"


REPO = Path(__file__).resolve().parent.parent


_LOCAL_PORT = re.compile(r"(127\.0\.0\.1|localhost):\d+")


def portable(value: Any) -> Any:
    """`value` with this machine's checkout and interpreter paths, and the
    ports a test's local servers happened to get, replaced — so an answer
    that names them compares on any machine and any run."""
    text = json.dumps(value)
    for path, name in ((sys.executable, "<python>"), (str(REPO), "<repo>")):
        text = text.replace(json.dumps(path)[1:-1], name)
    return json.loads(_LOCAL_PORT.sub(r"\1:<port>", text))


def _encode(value: Any) -> Any:
    if isinstance(value, tuple):
        return {"$tuple": [_encode(v) for v in value]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: _encode(v) for k, v in value.items()} if not any(k.startswith("$") for k in value) else {
                "$dict": [[k, _encode(v)] for k, v in value.items()]}
        return {"$dict": [[_encode(k), _encode(v)] for k, v in value.items()]}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, (set, frozenset)):
        return {"$set": sorted((_encode(v) for v in value), key=json.dumps)}
    if isinstance(value, Path):
        return {"$path": str(value)}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"can't record a {type(value).__name__}: {value!r}")


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        if len(value) == 1:
            [(tag, inner)] = value.items()
            if tag == "$tuple":
                return tuple(_decode(v) for v in inner)
            if tag == "$dict":
                return {_decode(k): _decode(v) for k, v in inner}
            if tag == "$bytes":
                return base64.b64decode(inner)
            if tag == "$datetime":
                return datetime.fromisoformat(inner)
            if tag == "$set":
                return {_decode(v) for v in inner}
            if tag == "$path":
                return Path(inner)
        return {k: _decode(v) for k, v in value.items()}
    return value


class _Store:
    """One module's recordings, keyed by test then call number."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, list[Any]] = json.loads(path.read_text()) if path.exists() else {}
        self.dirty = False

    def save(self) -> None:
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
            self.dirty = False


_stores: dict[str, _Store] = {}


class _Current:
    def __init__(self, store: _Store, test: str) -> None:
        self.store, self.test, self.n = store, test, 0


# Tests run one at a time; async fixtures and helpers run in tasks of their
# own, so this is a plain global rather than a context variable.
_current: _Current | None = None


@pytest.fixture(autouse=True)
def _python_golden(request):
    module = Path(request.node.fspath).stem
    store = _stores.get(module) or _stores.setdefault(module, _Store(GOLDEN_DIR / f"{module}.json"))
    global _current
    _current = _Current(store, request.node.name)
    yield
    _current = None
    store.save()


async def recorded(compute: Any = None) -> Any:
    """Python's side of the next comparison in this test (see the module docs)."""
    cur = _current
    assert cur is not None, "recorded() outside a test"
    n, cur.n = cur.n, cur.n + 1
    if RECORD:
        value = compute()
        if inspect.isawaitable(value):
            value = await value
        if n == 0:
            cur.store.data[cur.test] = []
        cur.store.data[cur.test].append(_encode(value))
        cur.store.dirty = True
        return _decode(_encode(value))
    calls = cur.store.data.get(cur.test)
    if calls is None or n >= len(calls):
        pytest.fail(f"no recording of Python's answer #{n} for {cur.test} in {cur.store.path.name}")
    return _decode(calls[n])
