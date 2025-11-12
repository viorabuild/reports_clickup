from datetime import datetime, timedelta

from clickup_agent.models import ClickUpTask
from clickup_agent.reports import DailyReportGenerator


class StubClickUpClient:
    def __init__(self, completed_tasks, active_tasks):
        self._completed_tasks = completed_tasks
        self._active_tasks = active_tasks

    def fetch_tasks(self, statuses, include_closed):
        return self._completed_tasks if include_closed else self._active_tasks


class DummySettings:
    pass


def test_tasks_without_due_date_are_included_in_statistics():
    target_date = datetime(2024, 5, 1)
    task_without_due = ClickUpTask(
        id="1",
        name="Задача без дедлайна",
        status="in progress",
        priority="high",
        due_date=None,
        url="https://example.com/task/1",
        assignees=[{"id": "42", "username": "Иван"}],
        time_estimate=3600000,
        time_spent=1800000,
        date_closed=None,
        date_created=target_date + timedelta(hours=1),
        date_updated=target_date + timedelta(hours=2),
    )

    clickup_client = StubClickUpClient(completed_tasks=[], active_tasks=[task_without_due])
    generator = DailyReportGenerator(clickup_client=clickup_client, settings=DummySettings())

    reports = generator.generate_reports(target_date=target_date)

    assert len(reports) == 1
    report = reports[0]

    assert len(report.not_completed_tasks) == 1
    task_stats = report.not_completed_tasks[0]
    assert task_stats.is_without_due_date is True
    assert task_stats.time_estimate_hours == 0.0
    assert task_stats.time_spent_hours == 0.0

    markdown = report.to_markdown()
    assert "⚠️ без дедлайна" in markdown
