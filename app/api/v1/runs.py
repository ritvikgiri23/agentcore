from typing import Any

from fastapi import APIRouter, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, PageParams
from app.api.v1 import API_V1_PREFIX
from app.core.errors import ErrorResponse
from app.models import AgentRun, RunStatus, RunStep
from app.repositories.ownership import get_owned_run
from app.schemas.pagination import Page
from app.schemas.runs import RunStatusResponse, RunStepResponse

router = APIRouter(prefix="/runs", tags=["runs"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token"},
    404: {"model": ErrorResponse, "description": "Run not found"},
}


def status_url(run_id: str) -> str:
    return f"{API_V1_PREFIX}{router.prefix}/{run_id}/status"


def stream_url(run_id: str) -> str:
    return f"{API_V1_PREFIX}{router.prefix}/{run_id}/stream"


@router.get(
    "/{run_id}/status",
    response_model=RunStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a run's status, token usage and, once completed, its final answer",
    responses=_ERRORS,
)
async def get_run_status(run_id: str, user: CurrentUser, db: DbSession) -> RunStatusResponse:
    run = await get_owned_run(db, user.id, run_id)
    return _status_response(run, await _count_steps(db, run.id))


async def _count_steps(db: AsyncSession, run_id: str) -> int:
    count = await db.scalar(
        select(func.count()).select_from(RunStep).where(RunStep.run_id == run_id)
    )
    return count or 0


def _status_response(run: AgentRun, step_count: int) -> RunStatusResponse:
    return RunStatusResponse(
        run_id=run.id,
        status=run.status,
        tokens_used=run.tokens_used,
        step_count=step_count,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        final_answer=run.final_answer if run.status == RunStatus.COMPLETED else None,
    )


@router.get(
    "/{run_id}/steps",
    response_model=Page[RunStepResponse],
    status_code=status.HTTP_200_OK,
    summary="List a run's steps in the order they happened",
    responses={
        **_ERRORS,
        422: {"model": ErrorResponse, "description": "Invalid limit or offset"},
    },
)
async def list_run_steps(
    run_id: str, user: CurrentUser, db: DbSession, paging: PageParams
) -> Page[RunStepResponse]:
    run = await get_owned_run(db, user.id, run_id)
    total = await _count_steps(db, run.id)
    steps = await db.scalars(
        select(RunStep)
        .where(RunStep.run_id == run.id)
        .order_by(RunStep.occurred_at, RunStep.id)
        .limit(paging.limit)
        .offset(paging.offset)
    )
    return Page(
        items=[RunStepResponse.model_validate(s) for s in steps],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )
