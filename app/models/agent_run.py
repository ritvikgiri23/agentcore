import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.agent_session import AgentSession


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})
TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class StepType(enum.StrEnum):
    """The fixed set of persisted step types. The stream's `done` event is never persisted."""

    MEMORY_RETRIEVAL = "memory_retrieval"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_TIMEOUT = "tool_timeout"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"
    CANCELLED = "cancelled"


class AgentRun(Base):
    """One user message submitted to a session, executed by a worker."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    user_message: Mapped[str] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status", values_callable=lambda e: [m.value for m in e]),
        default=RunStatus.QUEUED,
        index=True,
    )
    final_answer: Mapped[str | None] = mapped_column(Text, default=None)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    session: Mapped["AgentSession"] = relationship(back_populates="runs", lazy="raise")
    steps: Mapped[list["RunStep"]] = relationship(
        back_populates="run", lazy="raise", passive_deletes=True
    )


class RunStep(Base):
    """An append-only record of one thing that happened during a run."""

    __tablename__ = "run_steps"
    __table_args__ = (
        CheckConstraint(
            "step_type IN ({})".format(", ".join(f"'{t.value}'" for t in StepType)),
            name="ck_run_steps_step_type",
        ),
    )

    # A sequence, so steps sharing an occurrence time still order by insertion.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    step_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    run: Mapped[AgentRun] = relationship(back_populates="steps", lazy="raise")
