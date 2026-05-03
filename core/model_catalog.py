"""Model catalog — single source of truth for available LLM models.

The backend rejects /run and /automations requests whose `model` is not in
this catalog. The frontend fetches it via GET /models and populates all
selectors from the response — adding or removing a model requires a single
edit here, never any TSX.
"""

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
            from langchain_aws import ChatBedrock
            return ChatBedrock(model=model_name)

        if self.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(model_name=model_name, timeout=None, stop=None)

        raise ValueError(f"Unknown provider '{self.provider}' for model '{self.id}'")


AVAILABLE_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("google_genai:gemma-4-31b-it",           "Gemma 4 31B (Google)",      "google_genai"),
    ModelSpec("google_genai:gemma-4-26b-a4b-it",       "Gemma 4 26B (Google)",      "google_genai"),
    ModelSpec("google_genai:gemini-2.5-pro",           "Gemini 2.5 Pro (Google)",   "google_genai"),
    ModelSpec("google_genai:gemini-2.0-flash",         "Gemini 2.0 Flash (Google)", "google_genai"),
    ModelSpec("ollama:gemma4:26b",                     "Gemma 4 26B (Ollama)",  "ollama"),
    ModelSpec("ollama:llama3.3",                       "Llama 3.3 (Ollama)",    "ollama"),
    ModelSpec("ollama:qwen3:32b",                      "Qwen3 32B (Ollama)",    "ollama"),
    ModelSpec("bedrock:us.anthropic.claude-sonnet-4-6",                      "Claude Sonnet 4.6 (AWS Bedrock)",    "bedrock"),
    ModelSpec("anthropic:claude-opus-4-7",                                   "Claude Opus 4.7 (Anthropic)",        "anthropic"),
    ModelSpec("anthropic:claude-sonnet-4-6",                                 "Claude Sonnet 4.6 (Anthropic)",      "anthropic"),
    ModelSpec("anthropic:claude-haiku-4-5-20251001",                         "Claude Haiku 4.5 (Anthropic)",       "anthropic"),
)

DEFAULT_MODEL: str = AVAILABLE_MODELS[0].id

_AVAILABLE_IDS: frozenset[str] = frozenset(m.id for m in AVAILABLE_MODELS)


def is_valid_model(model: str) -> bool:
    """Whether `model` is in the catalog. Checked at write boundaries only —
    read paths stay permissive so old DB rows with stale model strings still
    run through `DEFAULT_MODEL` fallback in `build_agent`."""
    return model in _AVAILABLE_IDS
