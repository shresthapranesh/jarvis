"""A wrong SDK call teaches the right one.

The agent used to spend a `jarvis.help("<category>")` cell before every call
to learn a signature. Now a call whose arguments don't bind comes back with
the real signature and the docstring's head, so one failed cell corrects it.
Only binding errors are rewritten: a TypeError from inside a function is the
function's own and must reach the agent as it was raised.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from tools import sdk

# The calls below are wrong on purpose, as the agent's would be.
jarvis: Any = sdk


@pytest.fixture
def gate(monkeypatch):
    """The policy gate, recorded instead of read from a database."""
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(sdk, "_enforce_policy", lambda name, args: calls.append((name, args)))
    return calls


def test_an_unexpected_keyword_names_the_real_signature(gate):
    with pytest.raises(TypeError) as err:
        jarvis.read_artifact(artifact="a1")  # the parameter is `artifact_id`
    msg = str(err.value)
    assert msg.startswith("jarvis.read_artifact(): got an unexpected keyword argument 'artifact'")
    assert "Usage: jarvis.read_artifact(artifact_id" in msg
    assert "Markdown content of an artifact" in msg  # the docstring's head


def test_a_missing_argument_names_the_real_signature(gate):
    with pytest.raises(TypeError, match=r"missing a required argument: 'artifact_id'") as err:
        jarvis.read_artifact()
    assert "Usage: jarvis.read_artifact(" in str(err.value)


def test_too_many_positionals_names_the_real_signature(gate):
    with pytest.raises(TypeError, match="too many positional arguments") as err:
        jarvis.list_artifact_versions("a1", "extra")
    assert "Usage: jarvis.list_artifact_versions(artifact_id" in str(err.value)


def test_only_the_docstrings_head_is_quoted(gate):
    """The usage line is for fixing a call, not a second help() page."""
    def fn(x: int) -> None:
        """First line.
        Second line.
        Third line.

        A later paragraph."""

    with pytest.raises(TypeError) as err:
        sdk._policed(fn)(1, 2)
    msg = str(err.value)
    assert "First line." in msg and "Second line." in msg
    assert "Third line." not in msg and "later paragraph" not in msg


def test_a_type_error_from_inside_the_function_is_untouched(gate):
    def fn(x: int) -> None:
        """Raises its own TypeError."""
        raise TypeError("the function's own complaint")

    with pytest.raises(TypeError) as err:
        sdk._policed(fn)(1)
    assert str(err.value) == "the function's own complaint"
    assert "Usage:" not in str(err.value)


def test_the_gate_still_comes_first(monkeypatch):
    """A switched-off function says so, wrong arguments or not — the order the
    gate ran in before is unchanged."""
    def off(name, args):
        raise RuntimeError(f"jarvis.{name} is switched off")

    monkeypatch.setattr(sdk, "_enforce_policy", off)
    with pytest.raises(RuntimeError, match="switched off"):
        jarvis.read_artifact(artifact="a1")


def test_the_gate_sees_the_arguments_it_saw_before(gate):
    with pytest.raises(TypeError):
        jarvis.read_artifact("a1", nope=1)
    assert gate == [("read_artifact", {"nope": 1})]  # bind_partial failed → the keywords, as before


def test_help_still_shows_the_real_signature(monkeypatch):
    assert inspect.signature(sdk.read_artifact) == inspect.signature(jarvis.read_artifact.__wrapped__)
    monkeypatch.setattr(sdk, "_is_enabled", lambda name: True)
    assert "jarvis.read_artifact(artifact_id" in sdk.help("artifacts")
