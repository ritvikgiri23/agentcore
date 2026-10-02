"""Long-term memory: durable facts about a user, recalled by embedding similarity.

Every query is scoped to one user; nothing here can read or touch another user's memories.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LongTermMemory


@dataclass(frozen=True)
class RecalledMemory:
    memory: LongTermMemory
    # Cosine distance from the query: 0 is identical, 2 is opposite.
    distance: float


async def search(
    db: AsyncSession,
    user_id: str,
    embedding: list[float],
    *,
    limit: int,
    max_distance: float | None = None,
) -> list[RecalledMemory]:
    """The user's memories nearest the embedding, nearest first."""
    distance = LongTermMemory.embedding.cosine_distance(embedding)
    query = select(LongTermMemory, distance).where(LongTermMemory.user_id == user_id)
    if max_distance is not None:
        query = query.where(distance <= max_distance)
    rows = await db.execute(
        query.order_by(distance, LongTermMemory.created_at.desc()).limit(limit)
    )
    return [RecalledMemory(memory, float(d)) for memory, d in rows]


async def insert(
    db: AsyncSession,
    user_id: str,
    content: str,
    embedding: list[float],
    *,
    source_run_id: str | None,
) -> LongTermMemory:
    memory = LongTermMemory(
        user_id=user_id, content=content, embedding=embedding, source_run_id=source_run_id
    )
    db.add(memory)
    await db.commit()
    return memory


async def list_page(
    db: AsyncSession, user_id: str, *, limit: int, offset: int
) -> tuple[list[LongTermMemory], int]:
    """One page of the user's memories, newest first, and the user's total."""
    owned = LongTermMemory.user_id == user_id
    total = await db.scalar(select(func.count()).select_from(LongTermMemory).where(owned))
    memories = await db.scalars(
        select(LongTermMemory)
        .where(owned)
        .order_by(LongTermMemory.created_at.desc(), LongTermMemory.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(memories), total or 0
