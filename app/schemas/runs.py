from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from app.models import RunStatus, StepType
from app.schemas.sessions import reject_nul

MESSAGE_MAX_LENGTH = 8000


class RunCreate(BaseModel):
    message: Annotated[
        str,
        StringConstraints(min_length=1, max_length=MESSAGE_MAX_LENGTH),
        AfterValidator(reject_nul),
    ]


class RunAccepted(BaseModel):
    run_id: str
    status: RunStatus
    status_url: str
    stream_url: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: RunStatus
    tokens_used: int
    step_count: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    final_answer: str | None = None


class RunStepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    step_type: StepType
    payload: dict[str, Any]
    occurred_at: datetime
