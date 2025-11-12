from datetime import datetime, timedelta
from typing import Iterable, List

from clickup_agent.models import ClickUpTask
from clickup_agent.reports import DailyReportGenerator


class FakeClickUpClient:
    """Minimal ClickUp client stub for deterministic testing."""

    def __init__(
        self,
        completed_tasks: Iterable[ClickUpTask],
        active_tasks: Iterable[ClickUpTask],
    ) -> None:
        self.completed_tasks: List[ClickUpTask] = list(completed_tasks)
        self.active_tasks: List[ClickUpTask] = list(active_tasks)

    def fetch_tasks(self, *, include_closed: bool = False, **_: object) -> List[ClickUpTask]:
        return list(self.completed_tasks if include_closed else self.active_tasks)


def make_task(
    *,
    task_id: str,
    name: str,
    assignee_id: str | None,
    assignee_name: str | None,
    priority: str,
    time_estimate_hours: int,
    time_spent_hours: int,
    due_date: datetime | None,
    date_closed: datetime | None,
) -> ClickUpTask:
    assignees = []
    if assignee_id is not None:
        assignee: dict[str, str] = {"id": assignee_id}
        if assignee_name is not None:
            assignee["username"] = assignee_name
        assignees.append(assignee)

    return ClickUpTask(
        id=task_id,
        name=name,
        priority=priority,
        assignees=assignees,
        time_estimate=time_estimate_hours * 60 * 60 * 1000,
        time_spent=time_spent_hours * 60 * 60 * 1000,
        due_date=due_date,
        date_closed=date_closed,
    )


def extract_report_summary(reports):
    return [
        (
            report.employee_name,
            [task.name for task in report.completed_tasks],
            [task.name for task in report.not_completed_tasks],
        )
        for report in reports
    ]


def test_generate_reports_sorted_output_is_stable():
    target_date = datetime(2024, 5, 1)

    alice_tasks_completed = [
        make_task(
            task_id="a2",
            name="Zeta migration",
            assignee_id="101",
            assignee_name="Alice",
            priority="urgent",
            time_estimate_hours=3,
            time_spent_hours=4,
            due_date=target_date,
            date_closed=target_date + timedelta(hours=8),
        ),
        make_task(
            task_id="a1",
            name="Alpha integration",
            assignee_id="101",
            assignee_name="Alice",
            priority="normal",
            time_estimate_hours=2,
            time_spent_hours=2,
            due_date=target_date,
            date_closed=target_date + timedelta(hours=2),
        ),
    ]

    alice_tasks_active = [
        make_task(
            task_id="a4",
            name="Later due work",
            assignee_id="101",
            assignee_name="Alice",
            priority="normal",
            time_estimate_hours=1,
            time_spent_hours=0,
            due_date=target_date + timedelta(hours=3),
            date_closed=None,
        ),
        make_task(
            task_id="a3",
            name="Early due bugfix",
            assignee_id="101",
            assignee_name="Alice",
            priority="low",
            time_estimate_hours=1,
            time_spent_hours=0,
            due_date=target_date - timedelta(days=1),
            date_closed=None,
        ),
    ]

    charlie_tasks_completed = [
        make_task(
            task_id="c1",
            name="Charlie API",
            assignee_id="202",
            assignee_name="Charlie",
            priority="urgent",
            time_estimate_hours=5,
            time_spent_hours=6,
            due_date=target_date,
            date_closed=target_date + timedelta(hours=5),
        )
    ]

    charlie_tasks_active = [
        make_task(
            task_id="c2",
            name="Charlie review",
            assignee_id="202",
            assignee_name="Charlie",
            priority="normal",
            time_estimate_hours=2,
            time_spent_hours=0,
            due_date=target_date,
            date_closed=None,
        )
    ]

    client_one = FakeClickUpClient(
        completed_tasks=[*charlie_tasks_completed, *alice_tasks_completed],
        active_tasks=[*alice_tasks_active, *charlie_tasks_active],
    )
    generator_one = DailyReportGenerator(client_one, settings=object())
    reports_first = generator_one.generate_reports(target_date=target_date)

    # Reverse order of tasks to ensure input ordering changes
    client_two = FakeClickUpClient(
        completed_tasks=list(reversed([*alice_tasks_completed, *charlie_tasks_completed])),
        active_tasks=list(reversed([*charlie_tasks_active, *alice_tasks_active])),
    )
    generator_two = DailyReportGenerator(client_two, settings=object())
    reports_second = generator_two.generate_reports(target_date=target_date)

    assert extract_report_summary(reports_first) == [
        (
            "Alice",
            ["Alpha integration", "Zeta migration"],
            ["Early due bugfix", "Later due work"],
        ),
        ("Charlie", ["Charlie API"], ["Charlie review"]),
    ]

    assert extract_report_summary(reports_second) == extract_report_summary(reports_first)
