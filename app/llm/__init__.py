from app.core.config import Settings
from app.llm.base import (
    ChatMessage,
    ChatResult,
    Embedder,
    LLMProvider,
    LLMUsage,
    ToolCallRequest,
    ToolDefinition,
)
from app.llm.fake import FakeReply, HashEmbedder, ScriptedLLM

__all__ = [
    "ChatMessage",
    "ChatResult",
    "Embedder",
    "LLMProvider",
    "LLMUsage",
    "ToolCallRequest",
    "ToolDefinition",
    "create_embedder",
    "create_llm_provider",
]

# Until the keyless demo scenario exists, fake mode answers every message with this.
_FAKE_FALLBACK = FakeReply(content="This is the fake LLM. Configure OPENAI_API_KEY for real answers.")


def create_llm_provider(settings: Settings) -> LLMProvider:
    """The provider selected by settings. Callers own it and must `aclose()` it."""
    if settings.llm_provider == "openai":
        # Imported lazily so fake mode never needs the OpenAI client configured.
        from app.llm.openai_provider import OpenAIProvider

        assert settings.openai_api_key is not None
        return OpenAIProvider(
            api_key=settings.openai_api_key.get_secret_value(), model=settings.chat_model
        )
    return ScriptedLLM(fallback=_FAKE_FALLBACK)


def create_embedder(settings: Settings) -> Embedder:
    """The embedder selected by settings. Callers own it and must `aclose()` it."""
    if settings.llm_provider == "openai":
        from app.llm.openai_provider import OpenAIEmbedder

        assert settings.openai_api_key is not None
        return OpenAIEmbedder(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    return HashEmbedder(settings.embedding_dimensions)
