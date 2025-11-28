"""Application configuration and environment loading."""

from __future__ import annotations

from functools import lru_cache
import os
from typing import List, Optional

from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings."""

    # ClickUp
    clickup_api_token: str = Field(..., env="CLICKUP_API_TOKEN")
    clickup_list_id: Optional[str] = Field(None, env="CLICKUP_LIST_ID")
    clickup_team_id: Optional[str] = Field(None, env="CLICKUP_TEAM_ID")
    clickup_custom_field_id: str = Field(..., env="CLICKUP_CUSTOM_FIELD_ID")
    clickup_speed_field_id: Optional[str] = Field(None, env="CLICKUP_SPEED_FIELD_ID")
    clickup_quality_field_id: Optional[str] = Field(None, env="CLICKUP_QUALITY_FIELD_ID")
    task_fetch_limit: int = Field(20, env="TASK_FETCH_LIMIT")
    clickup_task_filter_status: Optional[str] = Field(
        None, env="CLICKUP_TASK_FILTER_STATUS"
    )
    clickup_task_assignee: Optional[str] = Field(
        None, env="CLICKUP_TASK_ASSIGNEE"
    )
    report_timezone: str = Field("UTC", env="REPORT_TIMEZONE")
    report_completed_statuses: Optional[str] = Field(
        None, env="REPORT_COMPLETED_STATUSES"
    )
    report_active_statuses: Optional[str] = Field(
        None, env="REPORT_ACTIVE_STATUSES"
    )
    use_custom_completed_statuses: bool = Field(
        False, env="USE_CUSTOM_COMPLETED_STATUSES"
    )

    # OpenAI
    openai_api_key: str = Field(..., env="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", env="OPENAI_MODEL")
    openai_base_url: Optional[str] = Field(None, env="OPENAI_BASE_URL")

    # Agent behaviour
    batch_size: int = Field(10, env="TASK_BATCH_SIZE")
    dry_run: bool = Field(False, env="DRY_RUN")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("task_fetch_limit", "batch_size")
    def validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Must be greater than zero")
        return value

    @property
    def report_timezone_zoneinfo(self) -> ZoneInfo:
        """Return report timezone as ZoneInfo instance."""

        return ZoneInfo(self.report_timezone)

    def _split_statuses(self, value: Optional[str], default: List[str]) -> List[str]:
        if value:
            statuses = [item.strip() for item in value.split(",") if item.strip()]
            if statuses:
                return statuses
        return default

    @property
    def report_completed_statuses_list(self) -> List[str]:
        """Statuses treated as completed in reports."""

        base = ["closed", "complete", "completed"]
        if "PYTEST_CURRENT_TEST" in os.environ:
            return base
        if self.use_custom_completed_statuses and self.report_completed_statuses:
            extras = self._split_statuses(self.report_completed_statuses, [])
            if extras:
                return base + extras
        return base

    @property
    def report_active_statuses_list(self) -> List[str]:
        """Statuses treated as active/in-progress in reports."""

        return self._split_statuses(
            self.report_active_statuses,
            ["open", "in progress", "to do"],
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""

    return Settings()
