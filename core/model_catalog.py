"""Model catalog — source of truth for available LLM models."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    id: str        # e.g. "google_genai:gemini-2.5-pro"
    label: str     # e.g. "Gemini 2.5 Pro"
    provider: str  # e.g. "google_genai"
    # Total input tokens the model accepts, or None when unknown. Consumed by
    # core.compaction.compact_threshold() to size the summarization trigger per
    # model instead of applying one flat number to a catalog whose windows span
    # two orders of magnitude.
    #
    # None is a safe answer, not a gap to fill with a guess: it falls back to the
    # conservative flat default. The error is asymmetric — a window set too small
    # only summarizes earlier than needed, while one set too large disables the
    # trigger until the provider rejects the call outright. Leave it None unless
    # the number is known.
    context_window: int | None = None

    def build_llm(self):
        """Instantiate the appropriate LangChain chat model for this spec."""
        model_name = self.id.partition(":")[2]

        if self.provider == "ollama":
            from langchain_ollama import ChatOllama
            return ChatOllama(model=model_name)

        if self.provider == "google_genai":
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(model=model_name)

        if self.provider == "bedrock":
            from langchain_aws import ChatBedrockConverse
            return ChatBedrockConverse(model=model_name)

        if self.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(model_name=model_name, timeout=None, stop=None)

        if self.provider == "openrouter":
            import os
            from langchain_openai import ChatOpenAI
            # OpenRouter speaks the OpenAI wire format, so the OpenAI client is
            # the whole integration — `model_name` is the upstream id verbatim
            # ("anthropic/claude-sonnet-4.5", "deepseek/deepseek-r1:free"). The
            # id splits on the FIRST colon, so a `:free`/`:nitro` variant suffix
            # survives into the model name intact.
            api_key = os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                raise ValueError(f"OPENROUTER_API_KEY is not set (required for '{self.id}')")
            return ChatOpenAI(
                model=model_name,
                base_url=OPENROUTER_BASE_URL,
                api_key=api_key,
                # NOT optional here: ChatOpenAI's `stream_usage` defaults to
                # None, which resolves to False, and every run in this app
                # streams. Without it `usage_metadata` is absent on streamed
                # calls and BudgetTracker/UsageAccumulator/PerfTracker all see
                # zero tokens — the budget would never trip and the perf badge
                # would read as unknown forever.
                stream_usage=True,
            )

        if self.provider == "meta":
            import os
            from langchain_meta import ChatMetaModel
            # The class's own env fallback is MODEL_API_KEY — too generic a
            # name to document; we standardize on META_API_KEY and fail with
            # a message that names it.
            api_key = os.environ.get("META_API_KEY")
            if not api_key:
                raise ValueError(f"META_API_KEY is not set (required for '{self.id}')")
            return ChatMetaModel(model=model_name, api_key=api_key)

        raise ValueError(f"Unknown provider '{self.provider}' for model '{self.id}'")


# Providers `build_llm` knows how to instantiate. A custom model's id must use
# one of these as its `provider:` prefix — there is no per-model code, only
# per-provider, so any model from one of these backends is supported.
KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {"ollama", "google_genai", "bedrock", "anthropic", "meta", "openrouter"}
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# OpenRouter fronts many upstreams behind one OpenAI-shaped API, so the provider
# alone doesn't say whether prompt caching applies — the *upstream* does, and it
# is the prefix of the model name. The `cache_control: ephemeral` blocks this app
# emits (core/context_cache.py) are Anthropic's spelling; OpenRouter forwards
# them to Anthropic upstreams and ignores them elsewhere, where caching is either
# automatic (OpenAI, DeepSeek) or absent. Marking a non-Anthropic route as cached
# wouldn't fail the call, it would just log cache stats that never materialize.
_OPENROUTER_CACHE_CONTROL_PREFIXES: tuple[str, ...] = ("anthropic/",)


def honors_cache_control(spec: "ModelSpec", enabled_providers: Iterable[str]) -> bool:
    """Whether to emit `cache_control` blocks for `spec`.

    `enabled_providers` is the operator-facing knob (`RunnerConfig.
    cache_enabled_providers`); this adds the one sub-provider rule the knob
    can't express, since an openrouter id names its upstream in the model name.
    """
    if spec.provider not in set(enabled_providers):
        return False
    if spec.provider == "openrouter":
        # A leading `~` marks an alias route (`~anthropic/claude-sonnet-latest`),
        # which resolves to the same upstream as the pinned ids beside it.
        name = spec.id.partition(":")[2].lower().lstrip("~")
        return name.startswith(_OPENROUTER_CACHE_CONTROL_PREFIXES)
    return True


# `context_window` is left None wherever the effective number isn't known — see
# the field comment on ModelSpec for why that's the safe value rather than a
# placeholder. Two cases deliberately stay None:
#   ollama:*  the model card's window is not what you get. ChatOllama sends no
#             `num_ctx`, so the server applies its own default (a few thousand
#             tokens on a stock install) and silently truncates above it. The
#             honest window here is whatever num_ctx is set to, which this
#             process can't see — so fall back to the flat default.
#   models at or past the knowledge cutoff, where a number would be invented.
BUILTIN_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("google_genai:gemma-4-31b-it",           "Gemma 4 31B (Google)",      "google_genai"),
    ModelSpec("google_genai:gemma-4-26b-a4b-it",       "Gemma 4 26B (Google)",      "google_genai"),
    ModelSpec("google_genai:gemini-2.5-pro",           "Gemini 2.5 Pro (Google)",   "google_genai", 1_048_576),
    ModelSpec("google_genai:gemini-2.0-flash",         "Gemini 2.0 Flash (Google)", "google_genai", 1_048_576),
    ModelSpec("google_genai:gemini-3.1-flash-lite",    "Gemini 3.1 Flash Lite (Google)", "google_genai"),
    ModelSpec("ollama:gemma4:26b",                     "Gemma 4 26B (Ollama)",  "ollama"),
    ModelSpec("ollama:llama3.3",                       "Llama 3.3 (Ollama)",    "ollama"),
    ModelSpec("ollama:qwen3:32b",                      "Qwen3 32B (Ollama)",    "ollama"),
    ModelSpec("bedrock:us.anthropic.claude-sonnet-4-6",                      "Claude Sonnet 4.6 (AWS Bedrock)",    "bedrock",  200_000),
    ModelSpec("anthropic:claude-opus-4-7",                                   "Claude Opus 4.7 (Anthropic)",        "anthropic"),
    ModelSpec("anthropic:claude-sonnet-4-6",                                 "Claude Sonnet 4.6 (Anthropic)",      "anthropic"),
    ModelSpec("anthropic:claude-haiku-4-5-20251001",                         "Claude Haiku 4.5 (Anthropic)",       "anthropic", 200_000),
    ModelSpec("meta:muse-spark-1.1",                                         "Muse Spark 1.1 (Meta)",              "meta"),
)

DEFAULT_MODEL: str = BUILTIN_MODELS[0].id


# In-memory cache of runtime-added models, hydrated from the DB. Populated at
# server startup (lifespan), refreshed on each GraphQL `models` query, and
# loaded per-invocation by the CLI (`_run_db`). See db/ops for persistence.
_custom_models: tuple[ModelSpec, ...] = ()


def provider_from_id(model_id: str) -> str:
    """The provider encoded in a `provider:model_name` id (text before ':')."""
    return model_id.partition(":")[0]


def set_custom_models(specs: Iterable[ModelSpec]) -> None:
    """Replace the in-memory custom-model cache."""
    global _custom_models
    _custom_models = tuple(specs)


def load_custom_models(rows: Iterable[dict]) -> None:
    """Hydrate the cache from raw config rows ({id, label, provider}).

    `provider` is inferred from the id prefix when absent; rows missing a
    usable id are skipped. An optional `context_window` is accepted so a
    runtime-added model can size its own compaction threshold; anything
    non-numeric or non-positive is dropped back to None (the flat default),
    since a bad window is worse than no window.
    """
    specs: list[ModelSpec] = []
    for r in rows:
        mid = r.get("id")
        if not mid:
            continue
        provider = r.get("provider") or provider_from_id(mid)
        try:
            window = int(r["context_window"])
        except (KeyError, TypeError, ValueError):
            window = 0
        specs.append(
            ModelSpec(mid, r.get("label") or mid, provider, window if window > 0 else None)
        )
    set_custom_models(specs)


def available_models() -> tuple[ModelSpec, ...]:
    """Built-in catalog plus runtime-added models, deduped by id (built-ins win)."""
    seen: set[str] = set()
    out: list[ModelSpec] = []
    for m in (*BUILTIN_MODELS, *_custom_models):
        if m.id in seen:
            continue
        seen.add(m.id)
        out.append(m)
    return tuple(out)


def get_model_spec(model_id: str) -> ModelSpec:
    """Return the catalog entry for `model_id`. Raises ValueError if absent."""
    spec = next((m for m in available_models() if m.id == model_id), None)
    if spec is None:
        raise ValueError(f"Unknown model '{model_id}'")
    return spec


def resolve_model_spec(model_id: str | None) -> ModelSpec:
    """The catalog entry for `model_id`, degrading to `DEFAULT_MODEL` instead of raising.

    A model id is stored on long-lived rows (an automation, a conversation, a
    board task, a workflow node) but the catalog is editable at runtime, so any
    of those rows can outlive the model it names — removing a custom model, or
    an install whose `models.custom` row was never migrated, leaves ids pointing
    at nothing. Read paths must not die for that: the run is what the operator
    asked for, the model was only how it was to be carried out.

    Sync, so this can only reach the compile-time seed. Anything with a DB
    session should call `db.ops.resolve_model` first — it falls back to the
    *operator's* `default.model`, and only what slips past it lands here.
    """
    try:
        return get_model_spec(model_id or "")
    except ValueError:
        logger.warning(
            "model %r is not in the catalog (removed?) — falling back to %s",
            model_id, DEFAULT_MODEL,
        )
        return get_model_spec(DEFAULT_MODEL)


def is_builtin_model(model_id: str) -> bool:
    """Whether `model_id` is compiled in — built-ins can't be edited or removed."""
    return any(m.id == model_id for m in BUILTIN_MODELS)


def is_valid_model(model: str) -> bool:
    """Whether `model` is in the catalog (built-in or custom).

    Checked at *write* boundaries — creating an automation, a board task, or
    pinning a conversation's model — where a bad id should be refused while the
    operator is still looking at it. Read paths stay permissive and degrade
    instead: `db.ops.resolve_model` for anything with a session,
    `resolve_model_spec` for the rest."""
    return any(m.id == model for m in available_models())
