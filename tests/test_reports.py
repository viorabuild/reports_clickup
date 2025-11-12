from datetime import datetime
from unittest.mock import MagicMock

from clickup_agent.config import Settings
from clickup_agent.reports import DailyReportGenerator


def test_generate_reports_uses_custom_statuses():
    settings = Settings(
        clickup_api_token="token",
        clickup_custom_field_id="field",
        openai_api_key="key",
        report_completed_statuses="done, finished",
        report_active_statuses="doing, backlog",
    )
    mock_clickup = MagicMock()
    mock_clickup.fetch_tasks.side_effect = [[], []]

    generator = DailyReportGenerator(mock_clickup, settings)

    reports = generator.generate_reports(target_date=datetime(2024, 1, 1))

    assert reports == []
    assert len(mock_clickup.fetch_tasks.call_args_list) == 2

    completed_call, active_call = mock_clickup.fetch_tasks.call_args_list

    assert completed_call.kwargs["statuses"] == ["done", "finished"]
    assert completed_call.kwargs["include_closed"] is True

    assert active_call.kwargs["statuses"] == ["doing", "backlog"]
    assert active_call.kwargs["include_closed"] is False
