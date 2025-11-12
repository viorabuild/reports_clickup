from datetime import datetime, timezone

from clickup_agent.config import Settings
from clickup_agent.models import ClickUpTask
from clickup_agent.reports import DailyReportGenerator


class _FakeClickUpClient:
    def __init__(self, completed_tasks, active_tasks=None):
        self._completed_tasks = completed_tasks
        self._active_tasks = active_tasks or []

    def fetch_tasks(
        self,
        *,
        limit=None,
        statuses=None,
        assignee=None,
        include_closed=False,
    ):
        del limit, statuses, assignee  # Unused in fake client
        if include_closed:
            return list(self._completed_tasks)
        return list(self._active_tasks)


def _build_settings(report_timezone: str) -> Settings:
    return Settings(
        clickup_api_token="token",
        clickup_custom_field_id="field",
        openai_api_key="key",
        report_timezone=report_timezone,
    )


def test_completed_task_around_midnight_respected_in_report_timezone():
    settings = _build_settings("Europe/Moscow")

    closing_time = datetime(2024, 1, 9, 21, 0, tzinfo=timezone.utc)

    task = ClickUpTask(
        id="1",
        name="Midnight task",
        description=None,
        status="closed",
        priority="high",
        due_date=None,
        url=None,
        assignees=[{"id": "emp1", "username": "Employee"}],
        time_estimate=3600000,
        time_spent=1800000,
        date_closed=closing_time,
        date_created=closing_time,
        date_updated=closing_time,
    )

    generator = DailyReportGenerator(
        _FakeClickUpClient(completed_tasks=[task]), settings
    )

    reports = generator.generate_reports(target_date=datetime(2024, 1, 10))

    assert len(reports) == 1
    report = reports[0]
    assert report.employee_id == "emp1"
    assert report.date.tzinfo is not None
    assert len(report.completed_tasks) == 1
    completed_task = report.completed_tasks[0]
    assert completed_task.name == "Midnight task"
    assert completed_task.is_completed is True
