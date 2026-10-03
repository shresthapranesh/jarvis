"""The transcript format — LangChain messages to and from v1 records.

See `core/transcript_format.md` for the format itself. `encode` turns a
message into a record (and the blobs its media parts point at); `decode`
turns a record back into an equal message. Lossless by construction: what
isn't mapped to a field of the format rides in `extras`, and comes back from
there.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

VERSION = 1

_ROLES: dict[str, type[BaseMessage]] = {
    "user": HumanMessage,
    "assistant": AIMessage,
    "tool": ToolMessage,
    "system": SystemMessage,
}
_ROLE_OF = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}

# langchain-google-genai keeps each tool call's thought signature here, keyed
# by tool call id.
_GEMINI_CALL_SIGNATURES = "__gemini_function_call_thought_signatures__"


class TranscriptError(ValueError):
    """A record this code can't read."""


@dataclass(frozen=True)
class Blob:
    hash: str
    mime_type: str
    data: bytes


# ── encode ───────────────────────────────────────────────────────────────────


def encode(msg: BaseMessage) -> tuple[dict[str, Any], list[Blob]]:
    """`msg` as a v1 record, and the blobs its media parts reference."""
    role = _ROLE_OF.get(msg.type)
    if role is None:
        raise TranscriptError(f"no transcript role for a {type(msg).__name__}")
    blobs: list[Blob] = []
    rec: dict[str, Any] = {"v": VERSION, "role": role}
    if msg.id is not None:
        rec["id"] = msg.id
    if msg.name is not None:
        rec["name"] = msg.name
    rec["content"] = _encode_content(msg.content, blobs)

    kwargs = dict(msg.additional_kwargs)
    metadata = dict(msg.response_metadata)
    if isinstance(msg, AIMessage):
        signatures = dict(kwargs.pop(_GEMINI_CALL_SIGNATURES, None) or {})
        calls = []
        for call in msg.tool_calls:
            out = {"id": call["id"], "name": call["name"], "args": call["args"]}
            if call["id"] in signatures:
                out["signature"] = signatures.pop(call["id"])
            calls.append(out)
        if calls:
            rec["tool_calls"] = calls
        if signatures:  # for calls this message no longer has
            kwargs[_GEMINI_CALL_SIGNATURES] = signatures
        invalid = [
            {k: c.get(k) for k in ("id", "name", "args", "error")} for c in msg.invalid_tool_calls
        ]
        if invalid:
            rec["invalid_tool_calls"] = invalid
        if msg.usage_metadata:
            rec["usage"] = _encode_usage(dict(msg.usage_metadata))
        model = {k: metadata.pop(src) for k, src in (("provider", "model_provider"), ("name", "model_name"))
                 if src in metadata}
        if model:
            rec["model"] = model
        if "finish_reason" in metadata:
            rec["finish_reason"] = metadata.pop("finish_reason")
    elif isinstance(msg, ToolMessage):
        rec["tool_call_id"] = msg.tool_call_id
        rec["status"] = msg.status
        if msg.artifact is not None:
            rec["artifact"] = msg.artifact

    extras = {k: v for k, v in (("additional_kwargs", kwargs), ("response_metadata", metadata)) if v}
    if extras:
        rec["extras"] = extras
    return rec, blobs


def _encode_usage(usage: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dst in (("input_tokens", "input"), ("output_tokens", "output"), ("total_tokens", "total"),
                     ("input_token_details", "input_details"), ("output_token_details", "output_details")):
        if src in usage:
            out[dst] = usage.pop(src)
    if usage:
        out["extras"] = usage
    return out


def _encode_content(content: Any, blobs: list[Blob]) -> Any:
    if isinstance(content, str):
        return content
    return [_encode_part(p, blobs) for p in content]


def _encode_part(part: Any, blobs: list[Blob]) -> Any:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        raise TranscriptError(f"content part is a {type(part).__name__}")
    rest = dict(part)
    kind = rest.pop("type", None)
    if kind == "text" and isinstance(rest.get("text"), str):
        out: dict[str, Any] = {"type": "text", "text": rest.pop("text")}
        # Gemini: {"extras": {"signature": ...}} on the part.
        inner = rest.get("extras")
        if isinstance(inner, dict) and isinstance(inner.get("signature"), str):
            inner = dict(inner)
            out["signature"] = inner.pop("signature")
            if inner:
                rest["extras"] = inner
            else:
                del rest["extras"]
        return _with_extras(out, rest)
    if kind == "thinking" and isinstance(rest.get("thinking"), str):
        out = {"type": "thinking", "thinking": rest.pop("thinking")}
        if isinstance(rest.get("signature"), str):
            out["signature"] = rest.pop("signature")
        return _with_extras(out, rest)
    if kind == "redacted_thinking" and isinstance(rest.get("data"), str):
        return _with_extras({"type": "redacted_thinking", "data": rest.pop("data")}, rest)
    if kind == "image_url":
        encoded = _encode_image_url(rest, blobs)
        if encoded is not None:
            return encoded
    if kind == "media" and isinstance(rest.get("mime_type"), str) and isinstance(rest.get("data"), str):
        mime = rest.pop("mime_type")
        out = {"type": "image" if mime.startswith("image/") else "file", "mime_type": mime}
        out.update(_encode_bytes(rest.pop("data"), mime, blobs))
        rest["lc_type"] = "media"
        return _with_extras(out, rest)
    return {"type": "opaque", "data": part}


def _encode_image_url(rest: dict[str, Any], blobs: list[Blob]) -> dict[str, Any] | None:
    """`{"type": "image_url", "image_url": "<url>" | {"url": ..., ...}}`."""
    value = rest.pop("image_url", None)
    shape = "str" if isinstance(value, str) else "dict"
    inner = {"url": value} if isinstance(value, str) else dict(value) if isinstance(value, dict) else None
    if inner is None or not isinstance(inner.get("url"), str):
        return None
    url = inner.pop("url")
    out: dict[str, Any] = {"type": "image"}
    if url.startswith("data:") and ";base64," in url:
        header, b64 = url[5:].split(";base64,", 1)
        out["mime_type"] = header
        out.update(_encode_bytes(b64, header, blobs))
    else:
        out["url"] = url
    rest["lc_type"] = "image_url"
    if shape == "str":
        rest["lc_image_url"] = "str"
    if inner:
        rest["lc_image_url_extras"] = inner
    return _with_extras(out, rest)


def _encode_bytes(b64: str, mime: str, blobs: list[Blob]) -> dict[str, Any]:
    """A blob reference, or the base64 inline when it wouldn't rebuild exactly."""
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return {"data": b64}
    if base64.b64encode(raw).decode() != b64:
        return {"data": b64}
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    blobs.append(Blob(digest, mime, raw))
    return {"blob": digest}


def _with_extras(out: dict[str, Any], rest: dict[str, Any]) -> dict[str, Any]:
    if rest:
        out["extras"] = rest
    return out


# ── decode ───────────────────────────────────────────────────────────────────


def decode(rec: Mapping[str, Any], blobs: Mapping[str, bytes] | None = None) -> BaseMessage:
    """The message `rec` records. `blobs` maps a part's `blob` to its bytes."""
    if rec.get("v") != VERSION:
        raise TranscriptError(f"transcript version {rec.get('v')!r} is not {VERSION}")
    cls = _ROLES.get(rec.get("role", ""))
    if cls is None:
        raise TranscriptError(f"unknown role {rec.get('role')!r}")
    extras = rec.get("extras") or {}
    fields: dict[str, Any] = {
        "content": _decode_content(rec.get("content", ""), blobs or {}),
        "additional_kwargs": dict(extras.get("additional_kwargs") or {}),
        "response_metadata": dict(extras.get("response_metadata") or {}),
    }
    if "id" in rec:
        fields["id"] = rec["id"]
    if "name" in rec:
        fields["name"] = rec["name"]
    if cls is AIMessage:
        calls = rec.get("tool_calls") or []
        signatures = {c["id"]: c["signature"] for c in calls if "signature" in c}
        if signatures:
            signatures.update(fields["additional_kwargs"].get(_GEMINI_CALL_SIGNATURES) or {})
            fields["additional_kwargs"][_GEMINI_CALL_SIGNATURES] = signatures
        fields["tool_calls"] = [
            {"id": c["id"], "name": c["name"], "args": c["args"], "type": "tool_call"} for c in calls
        ]
        fields["invalid_tool_calls"] = [
            {**c, "type": "invalid_tool_call"} for c in rec.get("invalid_tool_calls") or []
        ]
        if "usage" in rec:
            fields["usage_metadata"] = _decode_usage(rec["usage"])
        model = rec.get("model") or {}
        for key, dst in (("provider", "model_provider"), ("name", "model_name")):
            if key in model:
                fields["response_metadata"][dst] = model[key]
        if "finish_reason" in rec:
            fields["response_metadata"]["finish_reason"] = rec["finish_reason"]
    elif cls is ToolMessage:
        fields["tool_call_id"] = rec["tool_call_id"]
        fields["status"] = rec.get("status", "success")
        if "artifact" in rec:
            fields["artifact"] = rec["artifact"]
    return cls(**fields)


def _decode_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(usage.get("extras") or {})
    for src, dst in (("input", "input_tokens"), ("output", "output_tokens"), ("total", "total_tokens"),
                     ("input_details", "input_token_details"), ("output_details", "output_token_details")):
        if src in usage:
            out[dst] = usage[src]
    return out


def _decode_content(content: Any, blobs: Mapping[str, bytes]) -> Any:
    if isinstance(content, str):
        return content
    return [_decode_part(p, blobs) for p in content]


def _decode_part(part: Any, blobs: Mapping[str, bytes]) -> Any:
    if isinstance(part, str):
        return part
    kind = part.get("type")
    extras = dict(part.get("extras") or {})
    if kind == "opaque":
        return part["data"]
    if kind == "text":
        out: dict[str, Any] = {"type": "text", "text": part["text"], **extras}
        if "signature" in part:
            out["extras"] = {**(extras.get("extras") or {}), "signature": part["signature"]}
        return out
    if kind == "thinking":
        out = {"type": "thinking", "thinking": part["thinking"], **extras}
        if "signature" in part:
            out["signature"] = part["signature"]
        return out
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": part["data"], **extras}
    if kind in ("image", "file"):
        lc_type = extras.pop("lc_type", None)
        if lc_type == "image_url":
            shape = extras.pop("lc_image_url", "dict")
            inner = dict(extras.pop("lc_image_url_extras", None) or {})
            url = part["url"] if "url" in part else f"data:{part['mime_type']};base64,{_b64(part, blobs)}"
            value: Any = url if shape == "str" else {"url": url, **inner}
            return {"type": "image_url", "image_url": value, **extras}
        if lc_type == "media":
            return {"type": "media", "mime_type": part["mime_type"], "data": _b64(part, blobs), **extras}
        raise TranscriptError(f"an {kind} part with no LangChain spelling: {lc_type!r}")
    raise TranscriptError(f"unknown part type {kind!r}")


def _b64(part: Mapping[str, Any], blobs: Mapping[str, bytes]) -> str:
    if "data" in part:
        return part["data"]
    ref = part["blob"]
    if ref not in blobs:
        raise TranscriptError(f"missing blob {ref}")
    return base64.b64encode(blobs[ref]).decode()


# ── checking a checkpoints.db ────────────────────────────────────────────────


@dataclass
class CheckReport:
    messages: int = 0
    # (thread_id, message index, why) for each message that didn't come back equal.
    mismatches: list[tuple[str, int, str]] = field(default_factory=list)


def check_checkpoints(path: str, *, limit: int = 20) -> CheckReport:
    """Encode and decode every message in every checkpoint of a LangGraph
    `checkpoints.db`, through JSON as it would be stored, and report each one
    that doesn't come back equal. Opens the file read-only."""
    import json
    import sqlite3
    from pathlib import Path

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    report = CheckReport()
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT thread_id, type, checkpoint FROM checkpoints")
        for thread_id, kind, payload in rows:
            checkpoint = serde.loads_typed((kind, payload))
            for i, msg in enumerate((checkpoint.get("channel_values") or {}).get("messages") or []):
                report.messages += 1
                try:
                    rec, blobs = encode(msg)
                    back = decode(json.loads(json.dumps(rec)), {b.hash: b.data for b in blobs})
                    why = None if back == msg else f"{type(msg).__name__} differs"
                except Exception as exc:  # noqa: BLE001 — reported, not raised
                    why = f"{type(msg).__name__}: {exc}"
                if why and len(report.mismatches) < limit:
                    report.mismatches.append((thread_id, i, why))
    finally:
        conn.close()
    return report
