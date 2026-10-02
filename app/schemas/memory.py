from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    content: str
    source_run_id: str | None = Field(
        description="The run that created the memory; null once that run is deleted"
    )
    created_at: datetime


class MemorySearchResult(MemoryResponse):
    distance: float = Field(description="Cosine distance from the query: lower is more relevant")
