"""Expression / template engine for workflows — ADK prompt template analog.

Provides Jinja2-backed rendering with graceful fallback when jinja2 isn't installed.

Supported context (available inside {{ }}):
- Direct input vars: {{topic}} == {{inputs.topic}}
- Inputs namespace: {{inputs.foo}}
- Nodes namespace: {{nodes.node_id.output_key}} or {{nodes.node_id}} (whole output dict)
- Workflow namespace: {{workflow.foo}} (top-level workflow inputs)
- Any custom extra merged in

Legacy {{var}} placeholders still work (mapped to inputs[var]).

Filters available when Jinja2 is used:
- tojson, fromjson, upper, lower, trim, default, etc. (builtin Jinja + custom)
- json alias for tojson
- We also register json filter for convenience.

If Jinja2 missing, we fallback to regex that supports:
- {{var}} -> inputs[var]
- {{inputs.key}} -> inputs[key]
- {{nodes.id.port}} -> completed[id][port] or completed[id] as repr
- {{workflow.key}} -> workflow_inputs[key]
- Nested via dot splitting, with optional default via {{var | default("...")}} not supported in fallback (fallback keeps simple).

ContextVar _template_ctx holds the current workflow completed + workflow_inputs
so older call sites that only pass (template, inputs) can still resolve nodes.

Usage:
    from core.workflow_template import render_template, set_template_context

    rendered = render_template("Research {{topic}} {{nodes.n1.result}}", inputs, completed, workflow_inputs)
"""

from __future__ import annotations

import contextvars
import json
import re
from typing import Any

# Context var holding {"completed": dict, "workflow_inputs": dict}
_template_ctx_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "_workflow_template_ctx", default={}
)

# ── Jinja detection ───────────────────────────────────────────────────────

try:
    import jinja2  # type: ignore

    _HAS_JINJA = True
except ImportError:
    _HAS_JINJA = False
    jinja2 = None  # type: ignore


if _HAS_JINJA:
    # Create a reusable environment — import inside block for type-checker
    import jinja2 as _jinja2_mod

    _jinja_env = _jinja2_mod.Environment(
        undefined=_jinja2_mod.Undefined,  # missing -> empty string, but keeps rendering
        autoescape=False,
    )

    # Add extra filters
    def _filter_fromjson(s: Any) -> Any:
        if isinstance(s, str):
            try:
                return json.loads(s)
            except Exception:
                return s
        return s

    def _filter_json(s: Any, indent: int | None = None) -> str:
        try:
            if indent is not None:
                return json.dumps(s, indent=indent)
            return json.dumps(s)
        except Exception:
            return str(s)

    _jinja_env.filters["fromjson"] = _filter_fromjson
    _jinja_env.filters["json"] = _filter_json
    _jinja_env.filters["tojson"] = _filter_json
else:
    _jinja_env = None  # type: ignore


# ── Public context var helpers ────────────────────────────────────────────

def set_template_context(completed: dict[str, dict] | None = None, workflow_inputs: dict[str, Any] | None = None) -> contextvars.Token:
    """Set the template context var, return token for reset.

    Call in engine's _run_node before rendering.
    """
    ctx = {
        "completed": completed or {},
        "workflow_inputs": workflow_inputs or {},
    }
    return _template_ctx_var.set(ctx)


def reset_template_context(token: contextvars.Token) -> None:
    try:
        _template_ctx_var.reset(token)
    except Exception:
        pass


def get_template_context() -> dict[str, Any]:
    return _template_ctx_var.get({})


# ── Fallback regex renderer ───────────────────────────────────────────────

_var_pat = re.compile(r"\{\{\s*(.+?)\s*\}\}")

def _resolve_dotted(path: str, scope: dict[str, Any]) -> Any | None:
    """Resolve dotted path like 'nodes.n1.result' against scope dict."""
    parts = path.split(".")
    # Handle special namespaces
    if not parts:
        return None
    cur: Any = scope
    for p in parts:
        if isinstance(cur, dict):
            if p in cur:
                cur = cur[p]
            else:
                # Also try attribute-like if top-level namespace missing
                return None
        else:
            # cur not dict, can't drill further
            return None
    return cur


def _fallback_render(template: str, inputs: dict[str, Any], completed: dict[str, dict] | None, workflow_inputs: dict[str, Any] | None) -> str:
    # Build lookup scope for dotted resolution
    scope: dict[str, Any] = {}
    scope["inputs"] = inputs
    scope["nodes"] = completed or {}
    scope["workflow"] = workflow_inputs or {}
    # Also flatten inputs keys as top-level for {{var}} shorthand
    for k, v in inputs.items():
        if k not in scope:
            scope[k] = v

    def repl(m: re.Match) -> str:
        expr = m.group(1).strip()
        # Strip filters if present (e.g., var | default("x")) — we only support the var part in fallback
        # Split on pipe and take first part
        var_part = expr.split("|")[0].strip().strip("'\"")
        # Remove parentheses? Keep simple.
        # Try direct lookup
        if var_part in scope:
            val = scope[var_part]
            return str(val) if val is not None else ""
        # Try dotted resolution against scope
        resolved = _resolve_dotted(var_part, scope)
        if resolved is not None:
            # If resolved is dict and no deeper key, json dump? But return str repr for fallback
            if isinstance(resolved, dict):
                # For top-level node dict, return json-like? Use its string if not further drilled
                # But for {{nodes.id}} we want something; return json
                try:
                    return json.dumps(resolved)
                except Exception:
                    return str(resolved)
            return str(resolved)
        # Try legacy: inputs[var_part]
        if var_part in inputs:
            return str(inputs[var_part])
        # Not found -> keep placeholder
        return m.group(0)

    return _var_pat.sub(repl, template)


# ── Main render ───────────────────────────────────────────────────────────

def render_template(
    template: str,
    inputs: dict[str, Any] | None = None,
    completed: dict[str, dict] | None = None,
    workflow_inputs: dict[str, Any] | None = None,
) -> str:
    """Render a template string with Jinja2 if available, else fallback regex.

    Args:
        template: template with {{var}} placeholders
        inputs: current node inputs (edge-resolved)
        completed: dict[node_id -> output dict] for {{nodes.*}} references
        workflow_inputs: top-level workflow inputs for {{workflow.*}}
    """
    if not template:
        return template
    inputs = inputs or {}
    # Pull from context var if not explicitly passed
    ctx_var = get_template_context()
    if completed is None:
        completed = ctx_var.get("completed")
    if workflow_inputs is None:
        workflow_inputs = ctx_var.get("workflow_inputs")

    # Fast path: no braces -> return as is
    if "{{" not in template:
        return template

    if _HAS_JINJA and _jinja_env is not None:
        try:
            # Build Jinja context
            jinja_ctx: dict[str, Any] = {}
            # Flatten inputs keys for {{var}} shorthand
            jinja_ctx.update(inputs)
            jinja_ctx["inputs"] = inputs
            jinja_ctx["nodes"] = completed or {}
            jinja_ctx["workflow"] = workflow_inputs or {}
            # Also expose workflow_inputs keys flat under workflow? Already in workflow namespace.
            # For backward compat, expose completed nodes flat? No, keep namespaced.

            tmpl = _jinja_env.from_string(template)
            return tmpl.render(**jinja_ctx)
        except Exception:
            # On Jinja error, fall back to regex to avoid breaking workflows
            pass

    return _fallback_render(template, inputs, completed, workflow_inputs)


# ── Backward compat alias ─────────────────────────────────────────────────

def interpolate_legacy(template: str, inputs: dict[str, Any]) -> str:
    """Legacy regex {{var}} -> inputs[var] only, for code that cannot provide completed."""
    return _fallback_render(template, inputs, None, None)
