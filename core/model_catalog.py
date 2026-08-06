"""Model catalog — source of truth for available LLM models."""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str        # e.g. "google_genai:gemini-2.5-pro"
    label: str     # e.g. "Gemini 2.5 Pro"
    provider: str  # e.g. "google_genai"

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
    {"ollama", "google_genai", "bedrock", "anthropic", "meta"}
)


BUILTIN_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("google_genai:gemma-4-31b-it",           "Gemma 4 31B (Google)",      "google_genai"),
    ModelSpec("google_genai:gemma-4-26b-a4b-it",       "Gemma 4 26B (Google)",      "google_genai"),
    ModelSpec("google_genai:gemini-2.5-pro",           "Gemini 2.5 Pro (Google)",   "google_genai"),
    ModelSpec("google_genai:gemini-2.0-flash",         "Gemini 2.0 Flash (Google)", "google_genai"),
    ModelSpec("google_genai:gemini-3.1-flash-lite",    "Gemini 3.1 Flash Lite (Google)", "google_genai"),
    ModelSpec("ollama:gemma4:26b",                     "Gemma 4 26B (Ollama)",  "ollama"),
    ModelSpec("ollama:llama3.3",                       "Llama 3.3 (Ollama)",    "ollama"),
    ModelSpec("ollama:qwen3:32b",                      "Qwen3 32B (Ollama)",    "ollama"),
    ModelSpec("bedrock:us.anthropic.claude-sonnet-4-6",                      "Claude Sonnet 4.6 (AWS Bedrock)",    "bedrock"),
    ModelSpec("anthropic:claude-opus-4-7",                                   "Claude Opus 4.7 (Anthropic)",        "anthropic"),
    ModelSpec("anthropic:claude-sonnet-4-6",                                 "Claude Sonnet 4.6 (Anthropic)",      "anthropic"),
    ModelSpec("anthropic:claude-haiku-4-5-20251001",                         "Claude Haiku 4.5 (Anthropic)",       "anthropic"),
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
    usable id are skipped.
    """
    specs: list[ModelSpec] = []
    for r in rows:
        mid = r.get("id")
        if not mid:
            continue
        provider = r.get("provider") or provider_from_id(mid)
        specs.append(ModelSpec(mid, r.get("label") or mid, provider))
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


def is_builtin_model(model_id: str) -> bool:
    """Whether `model_id` is compiled in — built-ins can't be edited or removed."""
    return any(m.id == model_id for m in BUILTIN_MODELS)


def is_valid_model(model: str) -> bool:
    """Whether `model` is in the catalog (built-in or custom). Checked at write
    boundaries only — read paths stay permissive so old DB rows with stale model
    strings still run through `DEFAULT_MODEL` fallback in `build_agent`."""
    return any(m.id == model for m in available_models())
