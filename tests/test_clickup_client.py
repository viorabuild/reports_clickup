from __future__ import annotations

from typing import Dict

from unittest.mock import MagicMock

from clickup_agent.clickup import ClickUpClient
from clickup_agent.config import Settings


def _task_payload(task_id: int) -> Dict[str, object]:
    return {
        "id": str(task_id),
        "name": f"Task {task_id}",
        "status": {"status": "open"},
        "priority": {"priority": "normal"},
        "assignees": [],
    }


def test_fetch_tasks_paginates_until_limit(monkeypatch) -> None:
    settings = Settings(
        clickup_api_token="token",
        clickup_custom_field_id="custom",
        clickup_list_id="list",
        openai_api_key="sk-test",
        task_fetch_limit=10,
    )
    client = ClickUpClient(settings)

    responses = [
        {"tasks": [_task_payload(1), _task_payload(2), _task_payload(3)], "last_page": False},
        {"tasks": [_task_payload(4), _task_payload(5), _task_payload(6)], "last_page": True},
    ]

    mock_request = MagicMock(side_effect=responses)
    monkeypatch.setattr(client, "_request", mock_request)

    try:
        tasks = client.fetch_tasks(limit=5)
    finally:
        client.close()

    assert [task.id for task in tasks] == ["1", "2", "3", "4", "5"]
    assert mock_request.call_count == 2

    first_call = mock_request.call_args_list[0]
    second_call = mock_request.call_args_list[1]

    assert first_call.kwargs["params"]["page"] == 0
    assert first_call.kwargs["params"]["limit"] == 5
    assert second_call.kwargs["params"]["page"] == 1
    assert second_call.kwargs["params"]["limit"] == 5
