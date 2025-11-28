"""GPT analysis module for generating task recommendations."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from openai import APIError, OpenAI
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings
from .models import ClickUpTask, GPTRecommendation

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты опытный аналитик проектов. Твоя задача — изучать карточки ClickUp "
    "и предлагать конкретные рекомендации. Говори лаконично, по делу, "
    "на русском языке, избегай конфиденциальной информации. Ответ не длиннее 300 слов.\n"
    "Дополнительно оцени, сколько времени (в минутах) должна была занять задача, исходя из описания и приоритетов."
)


class GPTAnalysisError(Exception):
    """Raised when GPT analysis fails."""


class GPTAnalyzer:
    """Encapsulates interaction with OpenAI models."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        client_kwargs = {"api_key": settings.openai_api_key}
        if settings.openai_base_url:
            client_kwargs["base_url"] = settings.openai_base_url
        self._client = OpenAI(**client_kwargs)
        self._response_format = {"type": "json_object"}
        if settings.openai_base_url:
            # Локальные серверы OpenAI-совместимых моделей часто не поддерживают json_object
            self._response_format = {"type": "text"}

    def analyze(self, task: ClickUpTask) -> GPTRecommendation:
        """Generate a recommendation for a task."""

        prompt = self._build_prompt(task)
        logger.debug("Sending task %s to GPT", task.id)

        try:
            response_text = self._generate_response(prompt)
        except RetryError as exc:
            raise GPTAnalysisError("GPT API retry attempts exceeded.") from exc

        try:
            payload = json.loads(response_text)
            recommendation = GPTRecommendation(**self._normalize_payload(payload))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.error("Failed to parse GPT response: %s", response_text)
            raise GPTAnalysisError("Failed to parse GPT response.") from exc

        return recommendation

    def _build_prompt(self, task: ClickUpTask) -> str:
        description = task.description or "—"
        due_date = task.due_date.isoformat() if task.due_date else "не указан"
        status = task.status or "не указан"
        priority = task.priority or "не указан"
        url = task.url or "не указана"

        return (
            "Контекст: Ты аналитик проектов, анализирующий задачу.\n"
            "Данные задачи:\n"
            f"- Название: {task.name}\n"
            f"- Описание: {description}\n"
            f"- Статус: {status}\n"
            f"- Приоритет: {priority}\n"
            f"- Дедлайн: {due_date}\n"
            f"- Ссылка: {url}\n\n"
            "Задача: Проанализируй задачу и предоставь JSON с ключами:\n"
            "complexity — одна из: низкая, средняя, высокая.\n"
            "risks — массив кратких рисков.\n"
            "recommendations — массив рекомендаций по выполнению.\n"
            "optimizations — массив предложений по оптимизации.\n"
            "optimal_time_minutes — оценка оптимального времени выполнения в минутах (целое число).\n"
            "Если информации мало, делай разумные предположения и объясняй их в списках."
        )

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "complexity": payload.get("complexity", "").strip() or "не определена",
            "risks": self._normalize_list(payload.get("risks")),
            "recommendations": self._normalize_list(payload.get("recommendations")),
            "optimizations": self._normalize_list(payload.get("optimizations")),
            "optimal_time_minutes": self._parse_optional_int(
                payload.get("optimal_time_minutes")
                or payload.get("optimal_time")
                or payload.get("optimal_minutes")
            ),
        }

    @staticmethod
    def _normalize_list(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()]

    @retry(
        retry=retry_if_exception_type(APIError),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _generate_response(self, prompt: str) -> str:
        completion = self._client.chat.completions.create(
            model=self._settings.openai_model,
            temperature=0.2,
            response_format=self._response_format,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )

        message = completion.choices[0].message
        if not message.content:
            raise GPTAnalysisError("Empty response from GPT API.")
        return message.content

    @staticmethod
    def _parse_optional_int(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None
