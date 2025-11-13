"""Daily report generation for employee task statistics."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .clickup import ClickUpClient
from .config import Settings
from .models import ClickUpTask

logger = logging.getLogger(__name__)


class TaskStats(BaseModel):
    """Statistics for a single task."""

    name: str
    priority_emoji: str
    time_estimate_hours: float
    time_spent_hours: float
    is_completed: bool
    is_overdue: bool
    days_overdue: int = 0


class PriorityStats(BaseModel):
    """Statistics grouped by priority."""

    completed: int = 0
    not_completed: int = 0


class EmployeeReport(BaseModel):
    """Daily report for a single employee."""

    employee_id: str
    employee_name: str
    date: datetime
    completed_tasks: List[TaskStats] = Field(default_factory=list)
    not_completed_tasks: List[TaskStats] = Field(default_factory=list)
    total_planned_hours: float = 0.0
    total_actual_hours: float = 0.0
    priority_stats: Dict[str, PriorityStats] = Field(default_factory=dict)
    rescheduled_tasks: List[str] = Field(default_factory=list)
    overdue_tasks: List[str] = Field(default_factory=list)

    def to_markdown(self) -> str:
        """Render report as markdown."""
        lines = []
        date_str = self.date.strftime("%d.%m.%Y")

        # Header
        lines.append(f"📊 Отчёт за {date_str}")
        lines.append(f"👤 Сотрудник: {self.employee_name}")
        lines.append("")

        # Completed tasks
        lines.append(f"✅ Выполнено задач: {len(self.completed_tasks)}")
        if self.completed_tasks:
            for task in self.completed_tasks:
                time_info = f"(План: {task.time_estimate_hours:.1f}ч / Факт: {task.time_spent_hours:.1f}ч)"
                lines.append(f"  {task.name} {task.priority_emoji} {time_info}")
        else:
            lines.append("  Нет выполненных задач")
        lines.append("")

        # Time statistics
        time_diff = self.total_actual_hours - self.total_planned_hours
        time_diff_sign = "+" if time_diff >= 0 else ""
        lines.append("⏱️ Время:")
        lines.append(f"  Плановое: {self.total_planned_hours:.1f} ч")
        lines.append(f"  Фактическое: {self.total_actual_hours:.1f} ч")
        lines.append(f"  Разница: {time_diff_sign}{time_diff:.1f} ч")
        lines.append("")

        # Priority statistics
        lines.append("⚡ Срочность:")
        priority_order = [("Высокая", "urgent"), ("Средняя", "normal"), ("Низкая", "low")]
        for label, key in priority_order:
            stats = self.priority_stats.get(key, PriorityStats())
            lines.append(
                f"  {label}: выполнено {stats.completed}, не выполнено {stats.not_completed}"
            )
        lines.append("")

        # Rescheduled tasks
        if self.rescheduled_tasks:
            lines.append(f"📌 Перенесено задач по дедлайну: {len(self.rescheduled_tasks)}")
            for task_name in self.rescheduled_tasks:
                lines.append(f"  {task_name}")
            lines.append("")

        # Overdue tasks
        if self.overdue_tasks:
            lines.append(f"⏳ Просрочено более чем на 1 день: {len(self.overdue_tasks)}")
            for task_name in self.overdue_tasks:
                lines.append(f"  {task_name}")
            lines.append("")

        return "\n".join(lines)


class DailyReportGenerator:
    """Generates daily reports for all employees."""

    def __init__(self, clickup_client: ClickUpClient, settings: Settings) -> None:
        self._clickup = clickup_client
        self._settings = settings
        self._report_timezone = settings.report_timezone_zoneinfo

    def generate_reports(
        self,
        target_date: Optional[datetime] = None,
        statuses: Optional[List[str]] = None,
        assignee: Optional[str] = None,
    ) -> List[EmployeeReport]:
        """Generate daily reports for all employees."""
        target_day_local = self._normalize_target_date(target_date)
        next_day_local = target_day_local + timedelta(days=1)
        target_day_start_utc = target_day_local.astimezone(timezone.utc)
        next_day_utc = next_day_local.astimezone(timezone.utc)

        logger.info("Generating reports for date: %s", target_day_local.strftime("%Y-%m-%d"))

        # Fetch all tasks (completed and not completed)
        all_tasks = self._fetch_all_tasks(
            target_day_start_utc,
            next_day_utc,
            statuses=statuses,
            assignee=assignee,
        )

        if not all_tasks:
            logger.warning("No tasks found for the specified date")
            return []

        # Group tasks by employee
        employee_tasks = self._group_tasks_by_employee(all_tasks)

        # Generate reports for each employee
        reports = []
        for employee_id, tasks in self._iter_sorted_employee_tasks(employee_tasks):
            employee_name = self._extract_employee_name(employee_id, tasks)
            report = self._generate_employee_report(
                employee_id,
                employee_name,
                tasks,
                target_day_local,
                target_day_start_utc,
                next_day_utc,
            )
            reports.append(report)

        logger.info("Generated %d employee reports", len(reports))
        return reports

    def _fetch_all_tasks(
        self,
        target_day_start_utc: datetime,
        next_day_utc: datetime,
        *,
        statuses: Optional[List[str]] = None,
        assignee: Optional[str] = None,
    ) -> List[ClickUpTask]:
        """Fetch all tasks for the target date."""
        normalized_statuses = [
            status.strip() for status in (statuses or []) if status and status.strip()
        ]
        normalized_statuses_lower = {status.lower() for status in normalized_statuses}
        completed_statuses = self._settings.report_completed_statuses_list
        completed_statuses_lower = {status.lower() for status in completed_statuses}
        active_statuses = self._settings.report_active_statuses_list

        if normalized_statuses:
            fetched_tasks = self._clickup.fetch_tasks(
                statuses=normalized_statuses,
                assignee=assignee,
                include_closed=True,
            )
        else:
            # Fetch completed tasks
            completed_tasks = self._clickup.fetch_tasks(
                statuses=completed_statuses,
                assignee=assignee,
                include_closed=True,
            )

            # Fetch in-progress and open tasks
            active_tasks = self._clickup.fetch_tasks(
                statuses=active_statuses,
                assignee=assignee,
                include_closed=False,
            )

            fetched_tasks = [*completed_tasks, *active_tasks]

        # Filter tasks by date and provided criteria
        filtered_tasks: List[ClickUpTask] = []

        for task in fetched_tasks:
            if normalized_statuses_lower and (task.status or "").lower() not in normalized_statuses_lower:
                continue

            if assignee and not any(
                str(member.get("id")) == str(assignee) for member in task.assignees
            ):
                continue

            # Include if closed on target date
            closed_on_target_day = bool(
                task.date_closed
                and target_day_start_utc <= task.date_closed < next_day_utc
            )
            if closed_on_target_day:
                filtered_tasks.append(task)
                continue

            # Include if due date is on or before target date
            if task.due_date and task.due_date <= next_day_utc:
                filtered_tasks.append(task)
                continue

            # Include active tasks without due dates
            status_lower = (task.status or "").lower()
            if not task.due_date and status_lower not in completed_statuses_lower:
                filtered_tasks.append(task)

        return filtered_tasks

    def _group_tasks_by_employee(
        self, tasks: List[ClickUpTask]
    ) -> Dict[str, List[ClickUpTask]]:
        """Group tasks by employee ID."""
        employee_tasks: Dict[str, List[ClickUpTask]] = defaultdict(list)

        for task in tasks:
            if not task.assignees:
                # Assign to "Unassigned" group
                employee_tasks["unassigned"].append(task)
            else:
                for assignee in task.assignees:
                    employee_id = assignee.get("id", "unknown")
                    employee_tasks[employee_id].append(task)

        return employee_tasks

    def _generate_employee_report(
        self,
        employee_id: str,
        employee_name: str,
        tasks: List[ClickUpTask],
        target_day_local: datetime,
        target_day_start_utc: datetime,
        next_day_utc: datetime,
    ) -> EmployeeReport:
        """Generate report for a single employee."""
        next_day_local = target_day_local + timedelta(days=1)

        report = EmployeeReport(
            employee_id=employee_id,
            employee_name=employee_name,
            date=target_day_local,
        )

        # Initialize priority stats
        report.priority_stats = {
            "urgent": PriorityStats(),
            "normal": PriorityStats(),
            "low": PriorityStats(),
        }

        for task in tasks:
            # Determine if task is completed
            is_completed = bool(
                task.date_closed
                and target_day_start_utc <= task.date_closed < next_day_utc
            )

            # Calculate overdue status
            is_overdue = False
            days_overdue = 0
            due_local = task.due_date.astimezone(self._report_timezone) if task.due_date else None
            if due_local and due_local < target_day_local:
                is_overdue = True
                days_overdue = (target_day_local - due_local).days

            # Create task stats
            task_stat = TaskStats(
                name=task.name,
                priority_emoji=task.get_priority_emoji(),
                time_estimate_hours=task.get_time_estimate_hours(),
                time_spent_hours=task.get_time_spent_hours(),
                is_completed=is_completed,
                is_overdue=is_overdue,
                days_overdue=days_overdue,
            )

            # Add to appropriate list
            if is_completed:
                report.completed_tasks.append(task_stat)
                report.total_planned_hours += task_stat.time_estimate_hours
                report.total_actual_hours += task_stat.time_spent_hours
            else:
                report.not_completed_tasks.append(task_stat)

            # Update priority stats
            priority_key = self._normalize_priority(task.priority)
            if is_completed:
                report.priority_stats[priority_key].completed += 1
            else:
                report.priority_stats[priority_key].not_completed += 1

            # Check for rescheduled tasks (due date was today but not completed)
            if not is_completed and due_local:
                if target_day_local <= due_local < next_day_local:
                    report.rescheduled_tasks.append(task.name)

            # Check for overdue tasks (more than 1 day)
            if not is_completed and days_overdue > 1:
                report.overdue_tasks.append(task.name)

        return report

    def _normalize_target_date(self, target_date: Optional[datetime]) -> datetime:
        """Normalize target date to start of day in report timezone."""

        if target_date is None:
            base = datetime.now(tz=self._report_timezone)
        else:
            if target_date.tzinfo is None:
                base = target_date.replace(tzinfo=self._report_timezone)
            else:
                base = target_date.astimezone(self._report_timezone)
        return base.replace(hour=0, minute=0, second=0, microsecond=0)

    def _extract_employee_name(self, employee_id: str, tasks: List[ClickUpTask]) -> str:
        """Determine employee display name from tasks."""

        if employee_id == "unassigned":
            return "Без исполнителя"

        for task in tasks:
            for assignee in task.assignees:
                if str(assignee.get("id")) == str(employee_id):
                    return assignee.get("username") or assignee.get("email") or "Неизвестный"

        return "Неизвестный"

    def _iter_sorted_employee_tasks(
        self, employee_tasks: Dict[str, List[ClickUpTask]]
    ) -> List[tuple[str, List[ClickUpTask]]]:
        """Iterate over employee tasks in a deterministic order."""

        return sorted(
            employee_tasks.items(),
            key=lambda item: (
                self._extract_employee_name(item[0], item[1]).lower(),
                str(item[0]),
            ),
        )

    @staticmethod
    def _normalize_priority(priority: Optional[str]) -> str:
        """Normalize priority to standard keys."""
        if not priority:
            return "normal"
        priority_lower = priority.lower()
        if priority_lower in ["urgent", "high"]:
            return "urgent"
        elif priority_lower in ["normal", "medium"]:
            return "normal"
        else:
            return "low"
