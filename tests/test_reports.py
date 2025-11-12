from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from clickup_agent.config import Settings
from clickup_agent.models import ClickUpTask
from clickup_agent.reports import DailyReportGenerator


class DummyClickUpClient:
    """Test double mimicking ClickUpClient behaviour."""

    def __init__(self, tasks: List[ClickUpTask], respect_status_argument: bool) -> None:
        self._tasks = tasks
        self._respect_status_argument = respect_status_argument
        self.calls: List[dict] = []

    def fetch_tasks(
        self,
        *,
        limit: Optional[int] = None,
        statuses: Optional[Iterable[str]] = None,
        assignee: Optional[str] = None,
        include_closed: bool = False,
    ) -> List[ClickUpTask]:
        self.calls.append(
            {
                "limit": limit,
                "statuses": list(statuses) if statuses is not None else None,
                "assignee": assignee,
                "include_closed": include_closed,
            }
        )

        if self._respect_status_argument and statuses:
            status_set = {status.lower() for status in statuses if status}
            return [
                task
                for task in self._tasks
                if (task.status or "").lower() in status_set
            ]

        return list(self._tasks)

    def close(self) -> None:  # pragma: no cover - parity with ClickUpClient
        pass


def _make_settings() -> Settings:
    return Settings(
        clickup_api_token="token",
        clickup_custom_field_id="field",
        openai_api_key="key",
    )


def test_fetch_all_tasks_applies_status_and_assignee_filters() -> None:
    target_date = datetime(2024, 1, 10)
    tasks = [
        ClickUpTask(
            id="completed-mine",
            name="Completed task",
            status="completed",
            date_closed=target_date + timedelta(hours=3),
            assignees=[{"id": "123"}],
        ),
        ClickUpTask(
            id="progress-mine",
            name="In progress task",
            status="in progress",
            due_date=target_date + timedelta(hours=5),
            assignees=[{"id": "123"}],
        ),
        ClickUpTask(
            id="review-mine",
            name="Review task",
            status="review",
            due_date=target_date + timedelta(hours=5),
            assignees=[{"id": "123"}],
        ),
        ClickUpTask(
            id="completed-other",
            name="Completed other",
            status="completed",
            date_closed=target_date + timedelta(hours=1),
            assignees=[{"id": "999"}],
        ),
    ]

    dummy_client = DummyClickUpClient(tasks, respect_status_argument=False)
    generator = DailyReportGenerator(dummy_client, _make_settings())

    filtered_tasks = generator._fetch_all_tasks(  # type: ignore[attr-defined]
        target_date,
        statuses=["completed", "in progress"],
        assignee="123",
    )

    assert {task.id for task in filtered_tasks} == {"completed-mine", "progress-mine"}
    assert len(dummy_client.calls) == 1
    call = dummy_client.calls[0]
    assert set(call["statuses"]) == {"completed", "in progress"}
    assert call["assignee"] == "123"
    assert call["include_closed"] is True


def test_fetch_all_tasks_filters_by_assignee_when_statuses_not_provided() -> None:
    target_date = datetime(2024, 1, 10)
    tasks = [
        ClickUpTask(
            id="closed-today",
            name="Closed today",
            status="completed",
            date_closed=target_date + timedelta(hours=2),
            assignees=[{"id": "123"}],
        ),
        ClickUpTask(
            id="due-today",
            name="Due today",
            status="open",
            due_date=target_date + timedelta(hours=1),
            assignees=[{"id": "123"}],
        ),
        ClickUpTask(
            id="closed-previous",
            name="Closed previous",
            status="completed",
            date_closed=target_date - timedelta(days=1),
            assignees=[{"id": "123"}],
        ),
        ClickUpTask(
            id="due-future",
            name="Due future",
            status="in progress",
            due_date=target_date + timedelta(days=2),
            assignees=[{"id": "123"}],
        ),
        ClickUpTask(
            id="due-other-assignee",
            name="Due other",
            status="open",
            due_date=target_date + timedelta(hours=1),
            assignees=[{"id": "999"}],
        ),
    ]

    dummy_client = DummyClickUpClient(tasks, respect_status_argument=True)
    generator = DailyReportGenerator(dummy_client, _make_settings())

    filtered_tasks = generator._fetch_all_tasks(  # type: ignore[attr-defined]
        target_date,
        assignee="123",
    )

    assert {task.id for task in filtered_tasks} == {"closed-today", "due-today"}
    assert len(dummy_client.calls) == 2
    first_call, second_call = dummy_client.calls
    assert set(first_call["statuses"]) == {"closed", "complete", "completed"}
    assert first_call["assignee"] == "123"
    assert first_call["include_closed"] is True
    assert set(second_call["statuses"]) == {"open", "in progress", "to do"}
    assert second_call["assignee"] == "123"
    assert second_call["include_closed"] is False
