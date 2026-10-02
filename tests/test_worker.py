from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from app.runs.runner import RunInProgress
from app.worker import tasks


def test_enqueue_uses_the_run_id_as_the_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tasks.execute_agent_run, "apply_async", lambda **kwargs: sent.append(kwargs)
    )

    tasks.enqueue_run("run-123")

    assert sent == [{"args": ["run-123"], "task_id": "run-123"}]


def _raising(exc: Exception) -> Callable[[str], Coroutine[Any, Any, None]]:
    async def execute(run_id: str) -> None:
        raise exc

    return execute


def test_a_run_leased_elsewhere_is_checked_again_after_the_lease_could_expire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "_execute", _raising(RunInProgress("run-123", retry_in=42)))
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tasks.execute_agent_run, "apply_async", lambda **kwargs: sent.append(kwargs)
    )

    tasks.execute_agent_run.run("run-123")

    # A fresh message, not a retry: re-checks must not use up the retry budget.
    assert sent == [{"args": ["run-123"], "task_id": "run-123", "countdown": 43}]
