"""Short-term memory: a session's recent conversation turns, kept in a capped Redis list.

The list is newest first: each turn is pushed onto the head and the tail is trimmed off.
"""

import json
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from app.llm import ChatMessage


@dataclass(frozen=True)
class Turn:
    user: str
    assistant: str
    run_id: str
    ts: float

    def as_messages(self) -> list[ChatMessage]:
        """This turn as it goes back into the conversation: the question, then the answer."""
        return [
            {"role": "user", "content": self.user},
            {"role": "assistant", "content": self.assistant},
        ]


def history_key(session_id: str) -> str:
    return f"session:{session_id}:history"


async def push_turn(redis: Redis, session_id: str, turn: Turn, *, retained: int) -> None:
    """Add a turn and evict all but the `retained` most recent."""
    key = history_key(session_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.lpush(key, json.dumps(asdict(turn)))
        pipe.ltrim(key, 0, retained - 1)
        await pipe.execute()


async def recent_turns(redis: Redis, session_id: str, *, limit: int) -> list[Turn]:
    """Up to `limit` most recent turns, oldest first."""
    if limit <= 0:
        return []
    # redis-py types list commands as sync-or-async; on the asyncio client they are async.
    records: list[str] = await redis.lrange(  # type: ignore[misc]
        history_key(session_id), 0, limit - 1
    )
    return [Turn(**json.loads(record)) for record in reversed(records)]


async def clear(redis: Redis, session_id: str) -> None:
    await redis.delete(history_key(session_id))
