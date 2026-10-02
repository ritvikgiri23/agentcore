from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, DbSession, EmbedderDep, PageParams
from app.core.errors import ErrorResponse, ServiceUnavailableError
from app.memory import long_term
from app.repositories.ownership import get_owned_memory
from app.schemas.memory import MemoryResponse, MemorySearchResult
from app.schemas.pagination import Page

router = APIRouter(prefix="/memory", tags=["memory"])
logger = structlog.get_logger(__name__)

_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token"},
}

SEARCH_LIMIT = 5
MAX_QUERY_LENGTH = 2000


@router.get(
    "",
    response_model=Page[MemoryResponse],
    status_code=status.HTTP_200_OK,
    summary="List your long-term memories, newest first",
    responses={
        **_UNAUTHORIZED,
        422: {"model": ErrorResponse, "description": "Invalid limit or offset"},
    },
)
async def list_memories(
    user: CurrentUser, db: DbSession, paging: PageParams
) -> Page[MemoryResponse]:
    memories, total = await long_term.list_page(
        db, user.id, limit=paging.limit, offset=paging.offset
    )
    return Page(
        items=[MemoryResponse.model_validate(m) for m in memories],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get(
    "/search",
    response_model=list[MemorySearchResult],
    status_code=status.HTTP_200_OK,
    summary="Semantically search your long-term memories; the five nearest, nearest first",
    responses={
        **_UNAUTHORIZED,
        422: {"model": ErrorResponse, "description": "Missing or invalid query"},
        503: {"model": ErrorResponse, "description": "The query could not be embedded"},
    },
)
async def search_memories(
    q: Annotated[
        str,
        Query(min_length=1, max_length=MAX_QUERY_LENGTH, description="Plain-text query"),
    ],
    user: CurrentUser,
    db: DbSession,
    embedder: EmbedderDep,
) -> list[MemorySearchResult]:
    try:
        embedding = await embedder.embed(q)
    except Exception:
        logger.exception("memory_search_embedding_failed")
        raise ServiceUnavailableError("Memory search is unavailable; try again") from None
    # No distance cutoff: search is for inspecting what the agent could recall.
    recalled = await long_term.search(db, user.id, embedding, limit=SEARCH_LIMIT)
    return [
        MemorySearchResult(
            **MemoryResponse.model_validate(r.memory).model_dump(), distance=r.distance
        )
        for r in recalled
    ]


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete one of your long-term memories",
    responses={
        **_UNAUTHORIZED,
        404: {"model": ErrorResponse, "description": "Memory not found"},
    },
)
async def delete_memory(memory_id: str, user: CurrentUser, db: DbSession) -> None:
    memory = await get_owned_memory(db, user.id, memory_id)
    await db.delete(memory)
    await db.commit()
    logger.info("memory_deleted")
