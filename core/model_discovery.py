"""Provider model discovery — a lint over the catalog, not a replacement for it.

Every backend in `KNOWN_PROVIDERS` can enumerate its own models, and the records
carry most of what `ModelSpec` holds (notably `context_window`, which the catalog
otherwise leaves `None` and falls back to a flat default for). What discovery
*cannot* do is be the catalog:

- `BUILTIN_MODELS[0]` is the compile-time `DEFAULT_MODEL`. It has to resolve with
  no network, no key, and no provider outage.
- **Listing is not entitlement.** Measured against Google: `gemini-2.5-pro` is
  still returned by ListModels but 404s with "no longer available to new users"
  on an account that lacks it. The list reports what the provider publishes, not
  what this credential may call. Only `probe()` — a real one-token call —
  separates the two, so a listing alone can still hand you a dead model.

So this module reports drift and backfills metadata; deciding what the catalog
says stays with `BUILTIN_MODELS` + the `models.custom` runtime layer.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Providers whose discovery is implemented here. A provider in KNOWN_PROVIDERS
# but missing from this map is reported as unsupported rather than as "no models
# found", so a gap in this file can't read as a gap at the provider.
DISCOVERABLE: frozenset[str] = frozenset({"google_genai", "anthropic", "bedrock", "ollama"})

_TIMEOUT = 30.0


class DiscoveryError(RuntimeError):
    """Discovery could not run — missing credentials, unreachable host, etc.

    Distinct from "the provider returned zero models", which is a valid answer.
    """


@dataclass(frozen=True)
class DiscoveredModel:
    """One model as the provider describes it, normalized across backends."""

    id: str                      # full catalog id, e.g. "google_genai:gemini-2.5-flash"
    label: str                   # provider's display name, or the bare model name
    provider: str
    context_window: int | None = None   # input token limit, when the provider states one
    description: str | None = None
    # False when the name says this generates speech/images/music rather than
    # text. A hint for the operator, never a filter — see `looks_like_chat`.
    likely_chat: bool = True


# Substrings that mark a non-text generator. Google's ListModels publishes no
# output-modality field — `lyria-3-pro-preview` (music) and `gemini-2.5-flash-
# preview-tts` (speech) are structurally identical to `gemini-3.5-flash`, same
# supportedGenerationMethods and all — so the name is the only signal available.
#
# It is deliberately a *hint*, not a filter. A heuristic that silently dropped
# models would reintroduce the failure this module exists to warn about: a
# listing that disagrees with reality. Non-chat models stay in the report,
# flagged, and are skipped by --add-new unless explicitly included.
_NON_CHAT_MARKERS: tuple[str, ...] = (
    "-tts", "-image", "lyria", "nano-banana", "veo-", "imagen", "-embedding",
    "-live-", "computer-use", "robotics",
)


def looks_like_chat(model_name: str) -> bool:
    """Best-effort guess that a model generates text. Advisory only."""
    low = model_name.lower()
    return not any(mark in low for mark in _NON_CHAT_MARKERS)


# ── Per-provider adapters ────────────────────────────────────────────────────

def _discover_google() -> list[DiscoveredModel]:
    import httpx

    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise DiscoveryError("GOOGLE_API_KEY is not set")

    out: list[DiscoveredModel] = []
    page: str | None = None
    with httpx.Client(timeout=_TIMEOUT) as client:
        while True:
            params: dict[str, str | int] = {"pageSize": 1000}
            if page:
                params["pageToken"] = page
            r = client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key}, params=params,
            )
            if r.status_code != 200:
                raise DiscoveryError(f"ListModels failed ({r.status_code}): {r.text[:200]}")
            body = r.json()
            for m in body.get("models", []):
                # Filter to chat-capable models. The same endpoint also returns
                # embedding and token-counting models, which build_llm cannot use.
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                name = m["name"].split("/", 1)[-1]
                out.append(DiscoveredModel(
                    id=f"google_genai:{name}",
                    label=m.get("displayName") or name,
                    provider="google_genai",
                    context_window=m.get("inputTokenLimit"),
                    description=m.get("description"),
                    likely_chat=looks_like_chat(name),
                ))
            page = body.get("nextPageToken")
            if not page:
                return out


def _discover_anthropic() -> list[DiscoveredModel]:
    try:
        import anthropic
    except ImportError as exc:
        raise DiscoveryError(f"anthropic SDK not installed: {exc}") from exc
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise DiscoveryError("ANTHROPIC_API_KEY is not set")

    try:
        client = anthropic.Anthropic()
        # The models endpoint carries no context window, so context_window stays
        # None — the catalog's documented safe value, not a gap to guess at.
        return [
            DiscoveredModel(
                id=f"anthropic:{m.id}",
                label=getattr(m, "display_name", None) or m.id,
                provider="anthropic",
            )
            for m in client.models.list(limit=1000)
        ]
    except Exception as exc:
        raise DiscoveryError(f"models.list failed: {exc}") from exc


def _discover_bedrock() -> list[DiscoveredModel]:
    try:
        import boto3
    except ImportError as exc:
        raise DiscoveryError(f"boto3 not installed: {exc}") from exc

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    try:
        client = boto3.client("bedrock", region_name=region)
        resp = client.list_foundation_models(byOutputModality="TEXT")
    except Exception as exc:
        raise DiscoveryError(f"ListFoundationModels failed ({region}): {exc}") from exc

    out: list[DiscoveredModel] = []
    for m in resp.get("modelSummaries", []):
        mid = m.get("modelId")
        if not mid:
            continue
        # Only ON_DEMAND models are callable without a provisioned-throughput ARN.
        if "ON_DEMAND" not in (m.get("inferenceTypesSupported") or []):
            continue
        vendor = m.get("providerName") or "Bedrock"
        out.append(DiscoveredModel(
            id=f"bedrock:{mid}",
            label=f"{m.get('modelName') or mid} ({vendor})",
            provider="bedrock",
        ))
    return out


def _discover_ollama() -> list[DiscoveredModel]:
    import httpx

    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    if not host.startswith("http"):
        host = f"http://{host}"
    try:
        r = httpx.get(f"{host}/api/tags", timeout=_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        raise DiscoveryError(f"could not reach ollama at {host}: {exc}") from exc

    # context_window stays None on purpose: ChatOllama sends no `num_ctx`, so the
    # server's own default is what truncates, not the model card's window. See
    # the ModelSpec.context_window comment.
    return [
        DiscoveredModel(
            id=f"ollama:{m['name']}",
            label=f"{m['name']} (Ollama)",
            provider="ollama",
        )
        for m in r.json().get("models", []) if m.get("name")
    ]


_ADAPTERS = {
    "google_genai": _discover_google,
    "anthropic": _discover_anthropic,
    "bedrock": _discover_bedrock,
    "ollama": _discover_ollama,
}


def discover(provider: str) -> list[DiscoveredModel]:
    """Enumerate a provider's models. Raises DiscoveryError if it can't run.

    An empty list is a valid answer (a provider with nothing available);
    callers must not read it as failure.
    """
    fn = _ADAPTERS.get(provider)
    if fn is None:
        raise DiscoveryError(f"discovery not implemented for provider {provider!r}")
    return fn()


# ── Entitlement probe ────────────────────────────────────────────────────────

def probe(model_id: str) -> tuple[bool, str]:
    """Can this credential actually call this model? Returns (ok, detail).

    Listing is not entitlement — a model can be published and still 404 for a
    given account — so this issues a real one-token generation. It costs a
    request per model, which is why `model sync` only probes on request.
    """
    from core.model_catalog import get_model_spec

    try:
        spec = get_model_spec(model_id)
    except Exception:
        from core.model_catalog import ModelSpec, provider_from_id

        spec = ModelSpec(model_id, model_id, provider_from_id(model_id) or "")
    try:
        llm = spec.build_llm()
    except Exception as exc:
        return False, _first_line(str(exc))

    # Cap the reply where the provider accepts a cap, but never let the *cap*
    # decide the verdict. The kwarg is not portable — ChatGoogleGenerativeAI
    # rejects `max_tokens` outright ("Extra inputs are not permitted"), which
    # once made every Google model read as unreachable. A rejected cap is a
    # local signature problem, so retry uncapped and judge on that call.
    for kwargs in ({"max_tokens": 1}, {}):
        try:
            llm.invoke("hi", **kwargs)
            return True, ""
        except Exception as exc:
            if kwargs and _is_signature_error(exc):
                continue
            return False, _first_line(str(exc))
    return False, "probe did not complete"


def _is_signature_error(exc: Exception) -> bool:
    """True when the call was rejected for its arguments, not by the provider."""
    text = str(exc).lower()
    return (
        isinstance(exc, TypeError)
        or "extra inputs are not permitted" in text
        or "unexpected keyword" in text
        or "validation error" in text
    )


def _first_line(text: str, limit: int = 160) -> str:
    line = " ".join(text.split())
    return line[:limit] + ("…" if len(line) > limit else "")


# ── Drift report ─────────────────────────────────────────────────────────────

@dataclass
class SyncReport:
    provider: str
    missing: list[str]                              # in catalog, not offered by provider
    new: list[DiscoveredModel]                      # offered, not in catalog
    window_backfill: list[tuple[str, int]]          # (id, provider's inputTokenLimit)
    window_drift: list[tuple[str, int | None, int]] # (id, catalog value, provider value)
    unreachable: list[tuple[str, str]]              # (id, why) — only when probing

    @property
    def clean(self) -> bool:
        return not (self.missing or self.new or self.window_backfill
                    or self.window_drift or self.unreachable)


def build_report(provider: str, discovered: list[DiscoveredModel]) -> SyncReport:
    """Diff a provider's live models against the catalog."""
    from core.model_catalog import available_models

    catalog = {m.id: m for m in available_models() if m.provider == provider}
    live = {m.id: m for m in discovered}

    missing = sorted(set(catalog) - set(live))
    new = [live[i] for i in sorted(set(live) - set(catalog))]

    backfill: list[tuple[str, int]] = []
    drift: list[tuple[str, int | None, int]] = []
    for mid, spec in catalog.items():
        got = live.get(mid)
        if got is None or got.context_window is None:
            continue
        if spec.context_window is None:
            backfill.append((mid, got.context_window))
        elif spec.context_window != got.context_window:
            drift.append((mid, spec.context_window, got.context_window))

    return SyncReport(
        provider=provider, missing=missing, new=new,
        window_backfill=sorted(backfill), window_drift=sorted(drift), unreachable=[],
    )
