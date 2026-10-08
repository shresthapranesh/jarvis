"""Python's answers, recorded, for the tests that diff the Rust server.

These tests used to run every scenario on both servers and compare. The
Python server is gone, so what it answered was recorded while it still
existed (`tests/golden/python/<module>.json`, at the commit that removed it)
and each test compares the Rust server against that.

`await recorded()` returns the value stored for this call — the n-th
`recorded` call of the running test. Values keep their Python types: tuples,
bytes, datetimes and sets are tagged in the JSON.

A recording is Python's behaviour, frozen. A deliberate change to what the
server answers is a change to the recording: edit the JSON, and say why in
the commit.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "python"
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


_recordings: dict[str, dict[str, list[Any]]] = {}


class _Current:
    def __init__(self, module: str, test: str) -> None:
        if module not in _recordings:
            path = GOLDEN_DIR / f"{module}.json"
            _recordings[module] = json.loads(path.read_text()) if path.exists() else {}
        self.calls = _recordings[module].get(test)
        self.module, self.test, self.n = module, test, 0


# Tests run one at a time; async fixtures and helpers run in tasks of their
# own, so this is a plain global rather than a context variable.
_current: _Current | None = None


@pytest.fixture(autouse=True)
def _python_golden(request):
    global _current
    _current = _Current(Path(request.node.fspath).stem, request.node.name)
    yield
    _current = None


async def recorded() -> Any:
    """Python's side of the next comparison in this test."""
    cur = _current
    assert cur is not None, "recorded() outside a test"
    n, cur.n = cur.n, cur.n + 1
    if cur.calls is None or n >= len(cur.calls):
        pytest.fail(f"no recording of Python's answer #{n} for {cur.test} in {cur.module}.json")
    return _decode(cur.calls[n])
