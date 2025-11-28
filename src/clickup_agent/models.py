"""Pydantic models describing task inputs and GPT outputs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClickUpTask(BaseModel):
    """Subset of ClickUp task attributes used by the agent."""

    id: str
    name: str = Field(alias="name")
    description: Optional[str] = Field(None, alias="description")
    status: Optional[str] = Field(None, alias="status")
    priority: Optional[str] = Field(None, alias="priority")
    due_date: Optional[datetime] = Field(None, alias="due_date")
    url: Optional[str] = Field(None, alias="url")
    assignees: List[Dict[str, Any]] = Field(default_factory=list, alias="assignees")
    time_estimate: Optional[int] = Field(None, alias="time_estimate")  # milliseconds
    time_spent: Optional[int] = Field(None, alias="time_spent")  # milliseconds
    date_closed: Optional[datetime] = Field(None, alias="date_closed")
    date_created: Optional[datetime] = Field(None, alias="date_created")
    date_updated: Optional[datetime] = Field(None, alias="date_updated")

    @classmethod
    def from_api(cls, payload: dict) -> "ClickUpTask":
        """Create a task instance from ClickUp's API payload."""

        normalized = {
            "id": payload.get("id"),
            "name": payload.get("name", "").strip(),
            "description": (payload.get("description") or "").strip() or None,
            "status": (payload.get("status") or {}).get("status"),
            "priority": (payload.get("priority") or {}).get("priority"),
            "due_date": cls._parse_due_date(payload.get("due_date")),
            "url": payload.get("url"),
            "assignees": payload.get("assignees", []),
            "time_estimate": cls._parse_time(payload.get("time_estimate")),
            "time_spent": cls._parse_time(payload.get("time_spent")),
            "date_closed": cls._parse_timestamp(payload.get("date_closed")),
            "date_created": cls._parse_timestamp(payload.get("date_created")),
            "date_updated": cls._parse_timestamp(payload.get("date_updated")),
        }
        return cls(**normalized)

    @staticmethod
    def _parse_due_date(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            timestamp = int(value)
            if timestamp > 10**11:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None

    @staticmethod
    def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
        """Parse timestamp from ClickUp API (milliseconds)."""
        if not value:
            return None
        try:
            timestamp = int(value)
            if timestamp > 10**11:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None

    @staticmethod
    def _parse_time(value: Optional[str]) -> Optional[int]:
        """Parse time value from ClickUp API (milliseconds)."""
        if not value:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    def get_priority_emoji(self) -> str:
        """Get emoji for task priority."""
        priority_map = {
            "urgent": "🟥",
            "high": "🟥",
            "normal": "🟨",
            "medium": "🟨",
            "low": "🟩",
        }
        if not self.priority:
            return "⚪"
        return priority_map.get(self.priority.lower(), "⚪")

    def get_time_estimate_hours(self) -> float:
        """Get time estimate in hours."""
        if not self.time_estimate:
            return 0.0
        return self.time_estimate / (1000 * 60 * 60)

    def get_time_spent_hours(self) -> float:
        """Get time spent in hours."""
        if not self.time_spent:
            return 0.0
        return self.time_spent / (1000 * 60 * 60)


class GPTRecommendation(BaseModel):
    complexity: str
    risks: List[str]
    recommendations: List[str]
    optimizations: List[str]
    optimal_time_minutes: Optional[int] = None
    speed_score: Optional[int] = None
    quality_score: Optional[int] = None
    speed_reason: Optional[str] = None
    quality_reason: Optional[str] = None

    def to_markdown(self) -> str:
        """Render recommendation as markdown block for ClickUp custom field."""

        sections = []
        if self.speed_score is not None or self.speed_reason:
            sections.append(
                ("Оценка скорости", [self._format_score(self.speed_score, self.speed_reason)])
            )
        if self.quality_score is not None or self.quality_reason:
            sections.append(
                ("Оценка качества", [self._format_score(self.quality_score, self.quality_reason)])
            )
        sections.extend(
            [
                ("Оценка сложности", [self.complexity]),
                ("Потенциальные риски", self.risks),
                ("Рекомендации", self.recommendations),
                ("Оптимизации", self.optimizations),
            ]
        )

        lines = []
        for title, items in sections:
            lines.append(f"### {title}")
            if not items:
                lines.append("- —")
            else:
                for item in items:
                    lines.append(f"- {item.strip()}")
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _format_score(score: Optional[int], reason: Optional[str]) -> str:
        if score is None:
            return "—"
        reason_part = f" — {reason.strip()}" if reason else ""
        return f"{score}/5{reason_part}"


class TaskAnalysisResult(BaseModel):
    task: ClickUpTask
    recommendation: GPTRecommendation
    raw_response: Optional[str] = None
