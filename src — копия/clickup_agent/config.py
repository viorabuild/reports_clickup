"""Application configuration and environment loading."""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings."""

    # ClickUp
    clickup_api_token: str = Field(..., env="CLICKUP_API_TOKEN")
    clickup_list_id: Optional[str] = Field(None, env="CLICKUP_LIST_ID")
    clickup_team_id: Optional[str] = Field(None, env="CLICKUP_TEAM_ID")
    clickup_custom_field_id: str = Field(..., env="CLICKUP_CUSTOM_FIELD_ID")
    task_fetch_limit: int = Field(20, env="TASK_FETCH_LIMIT")
    clickup_task_filter_status: Optional[str] = Field(
        None, env="CLICKUP_TASK_FILTER_STATUS"
    )
    clickup_task_assignee: Optional[str] = Field(
        None, env="CLICKUP_TASK_ASSIGNEE"
    )

    # OpenAI
    openai_api_key: str = Field(..., env="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", env="OPENAI_MODEL")

    # Agent behaviour
    batch_size: int = Field(10, env="TASK_BATCH_SIZE")
    dry_run: bool = Field(False, env="DRY_RUN")
    report_timezone: str = Field("UTC", env="REPORT_TIMEZONE")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @field_validator("task_fetch_limit", "batch_size")
    def validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Must be greater than zero")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""

    return Settings()
