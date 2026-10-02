from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables (and `.env` if present)."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # General
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Infrastructure
    database_url: str = "postgresql+asyncpg://agentcore:agentcore@localhost:5432/agentcore"
    test_database_url: str = (
        "postgresql+asyncpg://agentcore:agentcore@localhost:5432/agentcore_test"
    )
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    jwt_secret: SecretStr = SecretStr("dev-insecure-secret-change-me")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=30, gt=0)

    # LLM provider. Falls back to the deterministic fake when no OpenAI key is set.
    openai_api_key: SecretStr | None = None
    llm_provider: Literal["openai", "fake"] | None = None
    chat_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # Agent loop tunables
    max_iterations: int = Field(default=10, gt=0)
    tool_timeout_seconds: float = Field(default=5.0, gt=0)
    tool_result_max_chars: int = Field(default=4096, gt=0)

    # Memory tunables
    short_term_context_messages: int = Field(default=10, ge=0)
    short_term_retained_turns: int = Field(default=20, gt=0)
    long_term_top_k: int = Field(default=3, ge=0)
    # Cosine distance ranges 0..2; a permissive default only drops clearly unrelated memories.
    memory_max_distance: float = Field(default=1.0, ge=0, le=2)
    memory_dedup_distance: float = Field(default=0.05, ge=0, le=2)

    # Runs
    max_active_runs_per_user: int = Field(default=10, gt=0)
    run_lease_ttl_seconds: int = Field(default=120, gt=0)

    @model_validator(mode="after")
    def _default_llm_provider(self) -> "Settings":
        if self.llm_provider is None:
            self.llm_provider = "openai" if self.openai_api_key else "fake"
        if self.llm_provider == "openai" and self.openai_api_key is None:
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
