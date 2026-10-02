from typing import Any

import pytest

from app.worker import tasks


def test_enqueue_uses_the_run_id_as_the_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tasks.execute_agent_run, "apply_async", lambda **kwargs: sent.append(kwargs)
    )

    tasks.enqueue_run("run-123")

    assert sent == [{"args": ["run-123"], "task_id": "run-123"}]
