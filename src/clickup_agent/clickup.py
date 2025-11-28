"""HTTP client wrapper for ClickUp API interactions."""

from __future__ import annotations

import json
import logging
from typing import Iterable, List, Optional

import httpx
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings
from .models import ClickUpTask

logger = logging.getLogger(__name__)


class ClickUpAPIError(Exception):
    """Raised when the ClickUp API returns a non-success response."""


class ClickUpClient:
    """Thin wrapper over ClickUp REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.Client(
            base_url="https://api.clickup.com/api/v2",
            headers={
                "Authorization": settings.clickup_api_token,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ClickUpClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[override]
        self.close()

    def fetch_tasks(
        self,
        *,
        limit: Optional[int] = None,
        statuses: Optional[Iterable[str]] = None,
        assignee: Optional[str] = None,
        include_closed: bool = False,
    ) -> List[ClickUpTask]:
        """Fetch tasks from ClickUp."""

        limit = limit or self._settings.task_fetch_limit
        if limit <= 0:
            return []
        params = {
            "archived": str(include_closed).lower(),
            "order_by": "updated",
            "reverse": "true",
            "subtasks": "true",
            "include_closed": str(include_closed).lower(),
        }

        if statuses:
            params["statuses[]"] = [
                status for status in statuses if str(status).strip()
            ]
        elif self._settings.clickup_task_filter_status:
            params["statuses[]"] = [
                s.strip()
                for s in self._settings.clickup_task_filter_status.split(",")
                if s.strip()
            ]

        if assignee:
            params["assignees[]"] = [assignee]
        elif self._settings.clickup_task_assignee:
            params["assignees[]"] = [self._settings.clickup_task_assignee]

        path = self._resolve_task_list_path()

        tasks: List[ClickUpTask] = []
        max_page_size = 100
        page_size = min(limit, max_page_size)
        page = 0

        while len(tasks) < limit:
            page_params = {
                **params,
                "page": page,
                "limit": page_size,
            }

            data = self._request("GET", path, params=page_params)
            tasks_payload = data.get("tasks", [])

            if not tasks_payload:
                break

            tasks.extend(ClickUpTask.from_api(task) for task in tasks_payload)

            if data.get("last_page"):
                break

            page += 1

        logger.info("Fetched %d tasks from ClickUp", len(tasks))
        return tasks[:limit]

    def update_task_custom_field(
        self,
        task_id: str,
        field_id: Optional[str],
        value: str,
    ) -> None:
        """Update the configured custom field on a task."""

        target_field = field_id or self._settings.clickup_custom_field_id
        if not target_field:
            raise ValueError("Custom field id must be provided.")

        path = f"/task/{task_id}/field/{target_field}"
        payload = {"value": value}

        if self._settings.dry_run:
            logger.info(
                "[dry-run] Would update task %s field %s with %s",
                task_id,
                target_field,
                value[:120],
            )
            return

        self._request("PUT", path, json=payload)

    def _resolve_task_list_path(self) -> str:
        if self._settings.clickup_list_id:
            return f"/list/{self._settings.clickup_list_id}/task"
        if self._settings.clickup_team_id:
            return f"/team/{self._settings.clickup_team_id}/task"
        raise ValueError(
            "Either CLICKUP_LIST_ID or CLICKUP_TEAM_ID must be configured."
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._retryable_request(method, path, **kwargs)
        except RetryError as exc:
            raise ClickUpAPIError("Exceeded retry attempts for ClickUp API") from exc

        if response.status_code >= 400:
            raise ClickUpAPIError(
                f"ClickUp API returned {response.status_code}: {response.text}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError:
            logger.debug("Non-JSON response received from ClickUp, returning empty dict.")
            return {}

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _retryable_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if response.status_code == 429:
            logger.warning(
                "Rate limited by ClickUp API, status 429. Retrying with backoff."
            )
            raise httpx.HTTPStatusError(
                "Rate limited", request=response.request, response=response
            )
        if response.status_code >= 500:
            logger.warning(
                "Server error from ClickUp API (%s). Retrying with backoff.",
                response.status_code,
            )
            raise httpx.HTTPStatusError(
                "Server error", request=response.request, response=response
            )
        return response
