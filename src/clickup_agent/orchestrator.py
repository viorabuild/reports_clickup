"""Coordinator for the ClickUp GPT recommendation workflow."""

from __future__ import annotations

import itertools
import logging
from typing import Iterable, List, Optional, Sequence

from .clickup import ClickUpClient, ClickUpAPIError
from .config import Settings, get_settings
from .gpt import GPTAnalyzer, GPTAnalysisError
from .models import ClickUpTask, TaskAnalysisResult

logger = logging.getLogger(__name__)


class TaskOrchestrator:
    """Coordinates fetching, analysis, and updating of ClickUp tasks."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        clickup_client: Optional[ClickUpClient] = None,
        analyzer: Optional[GPTAnalyzer] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._clickup = clickup_client or ClickUpClient(self._settings)
        self._analyzer = analyzer or GPTAnalyzer(self._settings)

    def run(
        self,
        *,
        statuses: Optional[Sequence[str]] = None,
        assignee: Optional[str] = None,
    ) -> List[TaskAnalysisResult]:
        """Execute the end-to-end processing pipeline."""

        try:
            tasks = self._clickup.fetch_tasks(
                statuses=statuses,
                assignee=assignee,
            )
        except (ClickUpAPIError, ValueError) as exc:
            logger.exception("Failed to fetch tasks from ClickUp.")
            raise

        if not tasks:
            logger.info("No tasks returned by ClickUp query.")
            return []

        results: List[TaskAnalysisResult] = []

        for chunk in self._chunk(tasks, self._settings.batch_size):
            logger.info("Processing batch of %d tasks", len(chunk))
            for task in chunk:
                try:
                    recommendation = self._analyzer.analyze(task)
                    rendered = recommendation.to_markdown()
                    self._clickup.update_task_custom_field(
                        task_id=task.id,
                        field_id=None,
                        value=rendered,
                    )
                    comment_body = self._build_comment(task, recommendation, rendered)
                    self._clickup.add_comment(task.id, comment_body, notify_all=False)
                    results.append(
                        TaskAnalysisResult(
                            task=task,
                            recommendation=recommendation,
                            raw_response=rendered,
                        )
                    )
                except GPTAnalysisError as exc:
                    logger.exception(
                        "GPT analysis failed for task %s: %s", task.id, exc
                    )
                except ClickUpAPIError as exc:
                    logger.exception(
                        "Failed to update ClickUp for task %s: %s", task.id, exc
                    )

        return results

    @staticmethod
    def _chunk(
        iterable: Sequence[ClickUpTask],
        size: int,
    ) -> Iterable[Sequence[ClickUpTask]]:
        it = iter(iterable)
        while True:
            batch = list(itertools.islice(it, size))
            if not batch:
                break
            yield batch

    def _build_comment(self, task: ClickUpTask, rec: GPTRecommendation, rendered: str) -> str:
        """Compose a human-friendly comment for ClickUp."""

        lines = [
            "AI-рекомендация по задаче:",
            f"Название: {task.name}",
            "",
            "Основные рекомендации:",
        ]
        if rec.recommendations:
            lines.extend([f"- {item}" for item in rec.recommendations])
        else:
            lines.append("- —")

        if rec.optimizations:
            lines.append("")
            lines.append("Оптимизации:")
            lines.extend([f"- {item}" for item in rec.optimizations])

        if rec.risks:
            lines.append("")
            lines.append("Риски:")
            lines.extend([f"- {item}" for item in rec.risks])

        if rec.optimal_time_minutes is not None:
            hours = rec.optimal_time_minutes / 60
            lines.append("")
            lines.append(
                f"Оценка оптимального времени: ~{rec.optimal_time_minutes} мин "
                f"({hours:.1f} ч)"
            )

        lines.append("")
        lines.append("Полный текст (для поля):")
        lines.append(rendered)

        return "\n".join(lines)
