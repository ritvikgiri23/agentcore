from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)


def _reject_nul(value: str) -> str:
    # Postgres text columns cannot store NUL characters.
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


class SessionCreate(BaseModel):
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        AfterValidator(_reject_nul),
    ]
    system_prompt: Annotated[
        str, StringConstraints(max_length=4000), AfterValidator(_reject_nul)
    ]
    tools_enabled: list[Annotated[str, StringConstraints(max_length=64)]] = Field(
        default_factory=list, max_length=50, description="Tool names; duplicates are ignored"
    )

    @field_validator("tools_enabled")
    @classmethod
    def _deduplicate(cls, tools: list[str]) -> list[str]:
        return list(dict.fromkeys(tools))


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    system_prompt: str
    tools_enabled: list[str]
    created_at: datetime


class RunSummary(BaseModel):
    id: str
    status: str
    message: str = Field(description="The run's user message, truncated to 120 characters")
    tokens_used: int
    created_at: datetime
    finished_at: datetime | None


class SessionDetail(SessionResponse):
    recent_runs: list[RunSummary] = Field(description="The five most recent runs, newest first")
