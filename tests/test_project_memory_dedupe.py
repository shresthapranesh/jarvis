"""Append-side dedup for project memory.

Project memory is injected uncached into every turn of every conversation in
the project, so a repeated `append` is a permanent per-turn tax. These drive the
pure helpers behind `jarvis.project_memory(action="append")` — no DB needed.
"""

from __future__ import annotations

from core.text_dedupe import dedupe_against, is_heading, normalize_entry

EXISTING = """## Stack
- This project uses FastAPI + Strawberry GraphQL on the backend
- Frontend is React 19 with Relay
- Package manager is pnpm, never npm
"""


def test_exact_and_near_duplicates_are_dropped():
    # second line is a reworded restatement, not a byte-for-byte repeat
    addition, dropped = dedupe_against(
        EXISTING,
        "- The project uses FastAPI + Strawberry GraphQL on the backend\n"
        "- Package manager is pnpm, never npm\n",
    )
    assert addition == ""
    assert dropped == 2


def test_new_facts_survive_alongside_duplicates():
    addition, dropped = dedupe_against(
        EXISTING,
        "- Frontend is React 19 with Relay\n"
        "- Tests run via `uv run pytest` against a throwaway WORK_DIR\n",
    )
    assert addition == "- Tests run via `uv run pytest` against a throwaway WORK_DIR"
    assert dropped == 1


def test_heading_survives_only_with_a_surviving_body():
    kept, _ = dedupe_against(
        EXISTING,
        "## Conventions\n- ForeignKey columns always carry index=True\n",
    )
    assert kept.startswith("## Conventions")

    orphaned, _ = dedupe_against(
        EXISTING, "## Stack\n- Package manager is pnpm, never npm\n"
    )
    assert orphaned == ""


def test_duplicates_within_one_append_are_collapsed():
    addition, dropped = dedupe_against(
        "", "- alpha beta gamma delta\n- alpha beta gamma delta\n"
    )
    assert addition == "- alpha beta gamma delta"
    assert dropped == 1


def test_short_entries_require_an_exact_match():
    # "ok" vs "oh" is a 0.5 ratio away from nothing — fuzzy matching short
    # strings is noise, so only the exact repeat is dropped.
    addition, dropped = dedupe_against("- ok\n", "- ok\n- oh\n")
    assert addition == "- oh"
    assert dropped == 1


def test_empty_memory_keeps_everything():
    body = "## Goals\n- Ship the board dispatcher\n"
    addition, dropped = dedupe_against("", body)
    assert addition == body.strip()
    assert dropped == 0


def test_normalization_ignores_markers_case_and_punctuation():
    assert normalize_entry("  1) Foo **bar**, baz.") == "foo bar baz"
    assert normalize_entry("- Foo bar baz") == normalize_entry("* foo  BAR baz!")


def test_heading_detection():
    assert is_heading("## Stack")
    assert is_heading("**Stack**")
    assert not is_heading("- uses **pnpm** for installs")
