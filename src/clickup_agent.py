import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

load_dotenv()

CLICKUP_API_BASE = "https://api.clickup.com/api/v2"


class ConfigError(RuntimeError):
    """Raised when required config is missing."""


@dataclass
class AgentConfig:
    api_token: str
    speed_field_id: str
    quality_field_id: str
    list_id: Optional[str] = None
    space_id: Optional[str] = None
    lm_base_url: str = "http://127.0.0.1:1234"
    lm_model: str = "openai/gpt-oss-20b"
    lm_temperature: float = 0.2
    target_statuses: Optional[List[str]] = None
    auto_close_statuses: Optional[List[str]] = None
    closed_status: Optional[str] = None
    max_tasks: Optional[int] = None
    history_log_path: str = "reports/assessments.md"
    history_limit: int = 5

    @property
    def normalized_target_statuses(self) -> Optional[List[str]]:
        if not self.target_statuses:
            return None
        return [status.strip().lower() for status in self.target_statuses if status.strip()]

    @property
    def normalized_auto_close_statuses(self) -> Optional[List[str]]:
        if not self.auto_close_statuses:
            return None
        return [status.strip().lower() for status in self.auto_close_statuses if status.strip()]

    @property
    def api_target_statuses(self) -> Optional[List[str]]:
        if not self.target_statuses:
            return None
        return [status.strip() for status in self.target_statuses if status.strip()]


@dataclass
class AssessmentResult:
    speed: int
    quality: int
    speed_reason: str
    quality_reason: str
    planned_time_minutes: Optional[int] = None
    tracked_time_minutes: Optional[int] = None
    optimal_time_minutes: Optional[int] = None
    time_estimate_realistic: Optional[bool] = None
    context_adjustment: Optional[float] = None
    trend: Optional[str] = None
    performer_level_match: Optional[bool] = None


def build_config() -> AgentConfig:
    required = {
        "CLICKUP_API_TOKEN": os.getenv("CLICKUP_API_TOKEN"),
        "CLICKUP_SPEED_FIELD_ID": os.getenv("CLICKUP_SPEED_FIELD_ID"),
        "CLICKUP_QUALITY_FIELD_ID": os.getenv("CLICKUP_QUALITY_FIELD_ID"),
    }

    list_id = os.getenv("CLICKUP_LIST_ID")
    space_id = os.getenv("CLICKUP_SPACE_ID")

    missing = [key for key, value in required.items() if not value]
    if missing:
        msg = ", ".join(missing)
        raise ConfigError(f"Missing required environment variables: {msg}")

    if bool(list_id) == bool(space_id):
        raise ConfigError(
            "Нужно указать ровно один источник задач: CLICKUP_LIST_ID или CLICKUP_SPACE_ID."
        )

    target_statuses = os.getenv("CLICKUP_TARGET_STATUSES")
    auto_close_statuses = os.getenv("CLICKUP_AUTO_CLOSE_STATUSES")

    def _split(value: Optional[str]) -> Optional[List[str]]:
        if not value:
            return None
        result = [item.strip() for item in value.split(",") if item.strip()]
        return result or None

    def _int_or_default(value: Optional[str], default: int) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"Невозможно преобразовать '{value}' в целое число.") from exc

    history_limit = _int_or_default(os.getenv("ASSESSMENT_HISTORY_LIMIT"), 5)
    max_tasks_value = _int_or_default(os.getenv("CLICKUP_MAX_TASKS"), 0)
    max_tasks = max_tasks_value if max_tasks_value > 0 else None
    history_log_path = os.getenv("ASSESSMENT_HISTORY_PATH", "reports/assessments.md")
    return AgentConfig(
        api_token=required["CLICKUP_API_TOKEN"],
        list_id=list_id,
        space_id=space_id,
        speed_field_id=required["CLICKUP_SPEED_FIELD_ID"],
        quality_field_id=required["CLICKUP_QUALITY_FIELD_ID"],
        lm_base_url=os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234"),
        lm_model=os.getenv("LM_STUDIO_MODEL", "openai/gpt-oss-20b"),
        lm_temperature=float(os.getenv("LM_TEMPERATURE", "0.2")),
        target_statuses=_split(target_statuses),
        auto_close_statuses=_split(auto_close_statuses),
        closed_status=os.getenv("CLICKUP_CLOSED_STATUS"),
        max_tasks=max_tasks,
        history_log_path=history_log_path,
        history_limit=history_limit,
    )


class ClickUpAgent:
    def __init__(self, config: AgentConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.project_root = Path(__file__).resolve().parent.parent
        history_path = Path(self.config.history_log_path)
        if not history_path.is_absolute():
            history_path = self.project_root / history_path
        self.history_path = history_path
        self.history_limit = max(0, self.config.history_limit)
        self.session = self._build_retry_session(
            headers={
                "Authorization": config.api_token,
                "Content-Type": "application/json",
            }
        )
        self.lm_session = self._build_retry_session(
            headers={
                "Content-Type": "application/json",
            }
        )
        self._task_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def run(self) -> None:
        tasks = self._fetch_tasks()
        if not tasks:
            logging.info("Нет подходящих задач для обработки.")
            return

        logging.info("Получено задач из ClickUp: %s", len(tasks))
        self._sort_tasks(tasks)
        prefetch_count = max(1, self.config.max_tasks or 1)
        self._prefetch_task_details(tasks[:prefetch_count])
        processed = 0
        for task in tasks:
            if self.config.max_tasks and processed >= self.config.max_tasks:
                break

            if not self._should_process_task(task):
                continue

            logging.info("Обработка задачи %s", task["name"])
            try:
                assessment = self._get_assessment(task)
                self._apply_assessment(task, assessment)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                logging.exception("Ошибка при обработке задачи %s: %s", task["id"], exc)

        logging.info("Готово. Обработано задач: %s", processed)

    def _fetch_tasks(self) -> List[Dict[str, Any]]:
        tasks: List[Dict[str, Any]] = []
        page = 0

        while True:
            params = {
                "page": page,
                "subtasks": "true",
                "include_closed": "true",
            }
            target_statuses = self.config.api_target_statuses
            if target_statuses:
                params["statuses[]"] = target_statuses
                params["include_closed"] = "false"

            url = self._tasks_endpoint()
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            current_tasks = payload.get("tasks", [])
            if not current_tasks:
                break

            tasks.extend(current_tasks)

            # Если задан лимит задач, не загружаем лишние страницы
            if self.config.max_tasks is not None and len(tasks) >= self.config.max_tasks:
                tasks = tasks[: self.config.max_tasks]
                break
            if not payload.get("last_page"):
                page += 1
            else:
                break

        return tasks

    def _prefetch_task_details(self, tasks: List[Dict[str, Any]]) -> None:
        if not tasks:
            return

        for task in tasks:
            task_id_raw = task.get("id")
            task_id = str(task_id_raw).strip() if task_id_raw else ""
            if not task_id:
                continue

            details = self._get_task_details(task_id)
            if not details:
                continue

            parent_id_raw = details.get("parent") or task.get("parent")
            parent_id = str(parent_id_raw).strip() if parent_id_raw else ""
            if parent_id:
                self._get_task_details(parent_id)

    def _sort_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        if not tasks:
            return

        def sort_key(task: Dict[str, Any]) -> tuple[int, int, str]:
            task_id = str(task.get("id") or "").strip()
            due_raw = task.get("due_date") or task.get("due_date_time")
            due_value = self._parse_clickup_timestamp(due_raw)

            closed_raw = task.get("date_closed") or task.get("date_closed")
            closed_value = self._parse_clickup_timestamp(closed_raw)

            due_key = due_value if due_value is not None else 2**63 - 1
            closed_key = closed_value if closed_value is not None else 2**63 - 1
            return (due_key, closed_key, task_id)

        tasks.sort(key=sort_key)

    def _tasks_endpoint(self) -> str:
        if self.config.list_id:
            return f"{CLICKUP_API_BASE}/list/{self.config.list_id}/task"
        if self.config.space_id:
            return f"{CLICKUP_API_BASE}/space/{self.config.space_id}/task"
        raise ConfigError("Не заданы CLICKUP_LIST_ID или CLICKUP_SPACE_ID")

    def _get_task_details(self, task_id: str) -> Optional[Dict[str, Any]]:
        if not task_id:
            return None
        if task_id in self._task_cache:
            return self._task_cache[task_id]
        url = f"{CLICKUP_API_BASE}/task/{task_id}"
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            logging.warning("Не удалось получить данные задачи %s: %s", task_id, exc)
            self._task_cache[task_id] = None
            return None
        data = response.json()
        self._task_cache[task_id] = data
        return data

    def _is_status_closed(self, task: Dict[str, Any]) -> bool:
        closed_status = (self.config.closed_status or "").strip().lower()
        if not closed_status:
            return False
        status_field = (task.get("status") or {}).get("status", "")
        current_status = str(status_field or "").strip().lower()
        return bool(current_status) and current_status == closed_status

    def _task_already_scored(self, task: Dict[str, Any]) -> bool:
        if not self.config.speed_field_id or not self.config.quality_field_id:
            return False
        return (
            self._custom_field_has_value(task, self.config.speed_field_id)
            and self._custom_field_has_value(task, self.config.quality_field_id)
        )

    @staticmethod
    def _custom_field_has_value(task: Dict[str, Any], field_id: str) -> bool:
        if not field_id:
            return False
        custom_fields = task.get("custom_fields")
        if not isinstance(custom_fields, list):
            return False
        target_id = str(field_id).strip()
        if not target_id:
            return False
        for field in custom_fields:
            candidate_id = str(field.get("id") or "").strip()
            if candidate_id != target_id:
                continue
            value = field.get("value")
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return True
        return False

    def _should_process_task(self, task: Dict[str, Any]) -> bool:
        status = (task.get("status") or {}).get("status", "").lower()
        targets = self.config.normalized_target_statuses
        if targets and status not in targets:
            logging.debug(
                "Пропуск задачи %s: статус '%s' не входит в целевые %s",
                task.get("id"),
                status,
                targets,
            )
            return False

        task_id = str(task.get("id") or "").strip()
        details = self._get_task_details(task_id) if task_id else None
        source = details or task
        if self._is_status_closed(source):
            logging.debug("Пропуск задачи %s: статус закрыт (%s)", task_id, self.config.closed_status)
            return False

        if self._task_already_scored(source):
            logging.debug("Пропуск задачи %s: кастомные поля скорости/качества уже заполнены", task_id)
            return False

        return True

    def _get_assessment(self, task: Dict[str, Any]) -> AssessmentResult:
        task_id = str(task.get("id") or "").strip()
        task_details = self._get_task_details(task_id) if task_id else None
        task_summary, time_metrics = self._build_task_context(task, details=task_details)
        source = task_details or task
        assignee_id, assignee_name, assignee_role = self._extract_primary_assignee(source)

        history_records = self._history_records_for_prompt(assignee_id)
        history_lines: List[str] = []
        for record in history_records:
            task_name = record.get("task_name", "Без названия")
            speed_norm = self._normalize_score(record.get("speed"))
            quality_norm = self._normalize_score(record.get("quality"))
            speed_reason = record.get("speed_reason", "")
            quality_reason = record.get("quality_reason", "")
            speed_display = f"{speed_norm:.2f}" if speed_norm is not None else "?"
            quality_display = f"{quality_norm:.2f}" if quality_norm is not None else "?"
            history_lines.append(
                f"- {task_name}: скорость {speed_display}/5 ({speed_reason}); "
                f"качество {quality_display}/5 ({quality_reason})"
            )

        avg_speed_history = self._calculate_metric_average(history_records, "speed")
        avg_quality_history = self._calculate_metric_average(history_records, "quality")
        avg_score = self._calculate_average_score(history_records)
        performer_category = self._get_performer_category(avg_score)

        history_section = ""
        if history_lines:
            history_title = "ИСТОРИЯ ИСПОЛНИТЕЛЯ"
            if assignee_name and assignee_name.lower() != "не указан":
                history_title += f" {assignee_name}"
            history_title += " (для контекстной корректировки)"
            history_section = "\n\n" + history_title + ":\n" + "\n".join(history_lines)

        avg_speed_history_display = (
            f"{avg_speed_history:.2f}" if avg_speed_history is not None else "нет данных"
        )
        avg_quality_history_display = (
            f"{avg_quality_history:.2f}" if avg_quality_history is not None else "нет данных"
        )
        avg_score_display = f"{avg_score:.2f}" if avg_score is not None else "нет данных"

        planned_minutes = time_metrics.get("planned_minutes")
        tracked_minutes = time_metrics.get("tracked_minutes")
        planned_display = str(planned_minutes) if planned_minutes is not None else "не указано"
        tracked_display = str(tracked_minutes) if tracked_minutes is not None else "не указано"
        time_coefficient: Optional[float] = None
        if planned_minutes and tracked_minutes and planned_minutes > 0:
            time_coefficient = tracked_minutes / planned_minutes
        time_coefficient_display = (
            f"{time_coefficient:.2f}" if time_coefficient is not None else "не рассчитан"
        )

        due_raw = source.get("due_date") or source.get("due_date_time")
        due_display = self._format_timestamp(due_raw)
        deadline_status = self._compute_deadline_status(source, due_raw)

        priority_obj = source.get("priority") or {}
        if isinstance(priority_obj, dict):
            priority_display = (
                priority_obj.get("priority")
                or priority_obj.get("label")
                or priority_obj.get("name")
                or "не указан"
            )
        else:
            priority_display = priority_obj or "не указан"

        task_type = (
            source.get("task_type")
            or source.get("type")
            or (source.get("custom_type") if isinstance(source.get("custom_type"), str) else None)
            or "не указан"
        )

        comments_count = source.get("comment_count")
        if comments_count is None:
            comments = source.get("comments")
            if isinstance(comments, list):
                comments_count = len(comments)
        activity_count = source.get("activity_count")

        errors_count = source.get("errors_count", "нет данных")
        rework_time = source.get("rework_time", "нет данных")
        acceptance_status = source.get("acceptance_status", "не указан")

        history_insights = {
            "avg_speed": avg_speed_history_display,
            "avg_quality": avg_quality_history_display,
            "avg_combined": avg_score_display,
            "performer_category": performer_category,
        }

        payload = {
            "model": self.config.lm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты ИИ-эксперт по оценке эффективности выполнения задач. "
                        "Твоя роль — объективно проанализировать задачу, внимательно изучив её контекст, "
                        "и предоставить независимую оценку скорости и качества.\n\n"
                        "МЕТОДОЛОГИЯ ОЦЕНКИ:\n\n"
                        "1. ОЦЕНКА СКОРОСТИ (1-5):\n"
                        "Рассчитай коэффициент K = Фактическое время / Плановое время\n"
                        "• 5 баллов: K ≤ 0,70 (выполнено на 30%+ быстрее плана, без ущерба качеству)\n"
                        "• 4 балла: K = 0,71-1,00 (в срок или с небольшим опережением)\n"
                        "• 3 балла: K = 1,01-1,30 (небольшое превышение до 30%, не критично)\n"
                        "• 2 балла: K = 1,31-1,60 (существенное превышение 31-60%, влияет на другие задачи)\n"
                        "• 1 балл: K > 1,60 (критическое превышение >60%, срыв дедлайна)\n\n"
                        "2. ОЦЕНКА КАЧЕСТВА (1-5):\n"
                        "• 5 баллов: 0 ошибок, 0 доработок, принято сразу, превосходит ожидания\n"
                        "• 4 балла: 1-2 незначительных замечания, исправлено быстро (<10% времени)\n"
                        "• 3 балла: 3-5 ошибок, доработка 10-30% времени, один раунд исправлений\n"
                        "• 2 балла: >5 ошибок или критичные, переработка 30-50% времени, несколько раундов\n"
                        "• 1 балл: критические ошибки, требуется полная переделка (>50% времени)\n\n"
                        "3. КОНТЕКСТНЫЙ АНАЛИЗ:\n"
                        "• Сравни текущую оценку со средним историческим баллом исполнителя\n"
                        "• Учитывай категорию исполнителя (эксперт/профессионал/развивающийся/проблемный)\n"
                        "• Определи тренд: прогресс (+), стабильность (=), или регресс (-)\n"
                        "• Для экспертов ожидания выше, для развивающихся — учитывай прогресс\n\n"
                        "4. ИИ-ОЦЕНКА ОПТИМАЛЬНОГО ВРЕМЕНИ:\n"
                        "• Проанализируй описание и тип задачи\n"
                        "• Оцени, сколько времени ДОЛЖНА была занять эта конкретная задача для специалиста данного уровня на основе всех доступных деталей\n"
                        "• Сравни с плановым временем: было ли оно реалистичным?\n"
                        "• Сравни с фактическим временем: насколько эффективно работал исполнитель?\n\n"
                        "ФОРМАТ ОТВЕТА:\n"
                        "Верни строго JSON с ключами:\n"
                        '• "speed_score": целое число 1-5\n'
                        '• "quality_score": целое число 1-5\n'
                        '• "speed_reason": краткое пояснение (до 50 слов) на русском\n'
                        '• "quality_reason": краткое пояснение (до 50 слов) на русском\n'
                        '• "optimal_time_minutes": твоя оценка оптимального времени для этой задачи\n'
                        '• "time_estimate_realistic": true/false — было ли плановое время реалистичным\n'
                        '• "context_adjustment": число от -1 до +1 — рекомендуемая корректировка на основе истории\n'
                        '• "trend": "progress" / "stable" / "regression" — тренд относительно истории\n'
                        '• "performer_level_match": true/false — соответствует ли результат уровню исполнителя\n\n'
                        "Будь объективным, учитывай все факторы, предоставляй конструктивную обратную связь."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "ЗАДАЧА НА ОЦЕНКУ:\n\n"
                        f"Название: {source.get('name') or task.get('name')}\n"
                        f"Описание: {task_summary}\n"
                        f"Тип задачи: {task_type}\n"
                        f"Приоритет: {priority_display}\n\n"
                        "ВРЕМЕННЫЕ ПОКАЗАТЕЛИ:\n"
                        f"Плановое время (estimate): {planned_display} минут\n"
                        f"Фактическое время (tracked): {tracked_display} минут\n"
                        f"Коэффициент K: {time_coefficient_display}\n"
                        f"Дедлайн: {due_display}\n"
                        f"Статус дедлайна: {deadline_status}\n\n"
                        "ПОКАЗАТЕЛИ КАЧЕСТВА:\n"
                        f"Количество ошибок: {errors_count}\n"
                        f"Время на доработки: {rework_time} минут\n"
                        f"Статус приемки: {acceptance_status}\n"
                        f"Количество комментариев: {comments_count if comments_count is not None else 'нет данных'}\n"
                        f"Количество изменений (activity): {activity_count if activity_count is not None else 'нет данных'}\n\n"
                        "ИСПОЛНИТЕЛЬ:\n"
                        f"Имя: {assignee_name}\n"
                        f"Роль/Специализация: {assignee_role}\n"
                        f"Категория: {performer_category}\n"
                        f"Средний исторический балл: {avg_score_display}\n"
                        f"Средний балл скорости (история): {avg_speed_history_display}\n"
                        f"Средний балл качества (история): {avg_quality_history_display}\n"
                        f"{history_section}\n\n"
                        "ТВОЯ ЗАДАЧА:\n"
                        "1. Оцени СКОРОСТЬ выполнения (1-5) на основе коэффициента K и контекста\n"
                        "2. Оцени КАЧЕСТВО результата (1-5) на основе ошибок, доработок и приемки\n"
                        "3. Определи ОПТИМАЛЬНОЕ ВРЕМЯ для этой задачи по твоему профессиональному мнению\n"
                        "4. Оцени, было ли плановое время реалистичным\n"
                        "5. Сравни с историей исполнителя и определи тренд (прогресс/стабильность/регресс)\n"
                        "6. Предложи контекстную корректировку (-1 до +1) на основе истории и категории\n"
                        "7. Оцени, соответствует ли результат уровню профессионализма исполнителя\n"
                        "8. Если у задачи есть плановое или фактическое время, подчеркни, "
                        "что расчёт оптимального времени выполнялся индивидуально для этой карточки\n\n"
                        "Ответь строго в JSON формате:\n"
                        '{\n'
                        '  "speed_score": 4,\n'
                        '  "quality_score": 5,\n'
                        '  "speed_reason": "Задача выполнена точно в срок (K=0.95), что соответствует плану...",\n'
                        '  "quality_reason": "Работа принята с первого раза без замечаний, 0 ошибок...",\n'
                        '  "optimal_time_minutes": 180,\n'
                        '  "time_estimate_realistic": true,\n'
                        '  "context_adjustment": 0.3,\n'
                        '  "trend": "progress",\n'
                        '  "performer_level_match": true\n'
                        '}'
                    ),
                },
            ],
            "temperature": self.config.lm_temperature,
            "max_tokens": 10000,
        }

        response_payload = self._call_lm_completion(payload)

        raw_content = (
            response_payload
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        content = self._extract_json_payload(raw_content)
        logging.debug("Ответ модели: %s", content)

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Модель вернула невалидный JSON: {content}") from exc

        speed = int(data["speed_score"])
        quality = int(data["quality_score"])
        speed_reason = str(data["speed_reason"]).strip()
        quality_reason = str(data["quality_reason"]).strip()
        speed_reason = self._truncate_words(speed_reason, 30)
        quality_reason = self._truncate_words(quality_reason, 30)

        planned_time = time_metrics.get("planned_minutes")
        tracked_time = time_metrics.get("tracked_minutes")

        optimal_time = self._parse_optional_int(data.get("optimal_time_minutes"))
        time_estimate_realistic = self._as_bool(data.get("time_estimate_realistic"))
        context_adjustment = self._parse_optional_float(data.get("context_adjustment"))
        trend = data.get("trend")
        if isinstance(trend, str):
            trend = trend.strip().lower()
        performer_level_match = self._as_bool(data.get("performer_level_match"))

        speed = max(1, min(5, speed))
        quality = max(1, min(5, quality))

        return AssessmentResult(
            speed=speed,
            quality=quality,
            speed_reason=speed_reason,
            quality_reason=quality_reason,
            planned_time_minutes=planned_time,
            tracked_time_minutes=tracked_time,
            optimal_time_minutes=optimal_time,
            time_estimate_realistic=time_estimate_realistic,
            context_adjustment=context_adjustment,
            trend=trend,
            performer_level_match=performer_level_match,
        )

    def _apply_assessment(self, task: Dict[str, Any], assessment: AssessmentResult) -> None:
        task_id = task["id"]
        logging.info(
            "Оценка задачи %s: скорость=%s, качество=%s",
            task_id,
            assessment.speed,
            assessment.quality,
        )

        if self.dry_run:
            logging.info("DRY RUN: пропуск обновления ClickUp для %s", task_id)
            return

        self._update_custom_fields(task_id, assessment)
        self._post_comment(task, assessment)
        self._append_history_entry(task, assessment)
        self._close_if_needed(task)

    def _update_custom_fields(self, task_id: str, assessment: AssessmentResult) -> None:
        # ClickUp надёжно обновляет кастомные поля через отдельный эндпоинт
        # POST /task/{task_id}/field/{field_id}, поэтому обновляем каждое поле по одному.

        def _update_field(field_id: str, value: int, label: str) -> None:
            if not field_id:
                return
            url = f"{CLICKUP_API_BASE}/task/{task_id}/field/{field_id}"
            payload = {"value": value}
            logging.info(
                "Обновление кастомного поля '%s' (%s) для задачи %s: payload=%s",
                label,
                field_id,
                task_id,
                payload,
            )
            response = self.session.post(url, json=payload, timeout=30)
            logging.info(
                "Ответ ClickUp по полю '%s' задачи %s: status=%s, body=%s",
                label,
                task_id,
                response.status_code,
                response.text,
            )
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                logging.error(
                    "Не удалось обновить кастомное поле '%s' для задачи %s: %s | Ответ: %s",
                    label,
                    task_id,
                    exc,
                    response.text,
                )
                raise

        if self.config.speed_field_id:
            _update_field(self.config.speed_field_id, assessment.speed, "speed")
        if self.config.quality_field_id:
            _update_field(self.config.quality_field_id, assessment.quality, "quality")

    def _post_comment(self, task: Dict[str, Any], assessment: AssessmentResult) -> None:
        task_id = task.get("id")
        if not task_id:
            logging.warning("Пропуск создания комментария: отсутствует идентификатор задачи.")
            return
        task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"
        comment_endpoint = f"{CLICKUP_API_BASE}/task/{task_id}/comment"
        body_parts = [
            "AI-оценка выполнения:",
            f"Скорость работы: {assessment.speed}/5 — {assessment.speed_reason}",
            f"Качество работы: {assessment.quality}/5 — {assessment.quality_reason}",
        ]
        time_lines = []
        if assessment.planned_time_minutes is not None:
            time_lines.append(
                f"Плановое время: {self._format_minutes(assessment.planned_time_minutes)}"
            )
        if assessment.tracked_time_minutes is not None:
            time_lines.append(
                f"Трекер времени: {self._format_minutes(assessment.tracked_time_minutes)}"
            )
        if time_lines:
            body_parts.extend(time_lines)
        if assessment.optimal_time_minutes is not None:
            body_parts.append(
                f"Оптимальное время (оценка ИИ): {self._format_minutes(assessment.optimal_time_minutes)}"
            )
        if assessment.time_estimate_realistic is not None:
            body_parts.append(
                "Плановое время реалистично: "
                f"{'да' if assessment.time_estimate_realistic else 'нет'}"
            )
        if assessment.context_adjustment is not None:
            body_parts.append(
                f"Контекстная корректировка: {assessment.context_adjustment:+.2f}"
            )
        if assessment.trend:
            body_parts.append(
                f"Тренд исполнителя: {self._translate_trend(assessment.trend)}"
            )
        if assessment.performer_level_match is not None:
            body_parts.append(
                "Соответствует уровню: "
                f"{'да' if assessment.performer_level_match else 'нет'}"
            )
        body_parts.append(f"Ссылка на задачу: {task_url}")
        body = "\n".join(body_parts)
        payload = {"comment_text": body, "notify_all": False}
        response = self.session.post(comment_endpoint, json=payload, timeout=30)
        response.raise_for_status()

    def _history_records_for_prompt(
        self,
        assignee_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if self.history_limit <= 0:
            return []
        if not self.history_path.exists():
            return []
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logging.warning("Не удалось прочитать файл истории оценок: %s", exc)
            return []
        records: List[Dict[str, Any]] = []
        for line in lines:
            stripped = line.strip()
            if not (stripped.startswith("<!--") and stripped.endswith("-->")):
                continue
            payload = stripped[4:-3].strip()
            if not payload:
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                logging.warning("Пропуск поврежденной записи истории: %s", payload)
                continue
            records.append(data)
        filtered = records
        normalized_assignee_id = (
            str(assignee_id).strip() if assignee_id not in (None, "") else ""
        )
        if normalized_assignee_id:
            filtered = [
                record
                for record in records
                if str(record.get("assignee_id") or "").strip() == normalized_assignee_id
            ]
        if not filtered:
            return []
        return filtered[-self.history_limit :]

    def _append_history_entry(self, task: Dict[str, Any], assessment: AssessmentResult) -> None:
        if self.dry_run:
            logging.info("DRY RUN: пропуск сохранения истории для задачи %s", task.get("id"))
            return
        task_id = task.get("id")
        if not task_id:
            logging.warning("Пропуск сохранения истории: отсутствует идентификатор задачи.")
            return
        task_name = (task.get("name") or f"Задача {task_id}").strip()
        task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        assignee_id, assignee_name, _ = self._extract_primary_assignee(task)
        record = {
            "task_id": task_id,
            "task_name": task_name,
            "task_url": task_url,
            "speed": assessment.speed,
            "speed_reason": assessment.speed_reason,
            "quality": assessment.quality,
            "quality_reason": assessment.quality_reason,
            "planned_minutes": assessment.planned_time_minutes,
            "tracked_minutes": assessment.tracked_time_minutes,
            "optimal_time_minutes": assessment.optimal_time_minutes,
            "time_estimate_realistic": assessment.time_estimate_realistic,
            "context_adjustment": assessment.context_adjustment,
            "trend": assessment.trend,
            "performer_level_match": assessment.performer_level_match,
            "assignee_id": assignee_id,
            "assignee_name": assignee_name,
            "timestamp": timestamp,
        }
        entry_lines = [
            f"<!-- {json.dumps(record, ensure_ascii=False)} -->",
            f"## [{task_name}]({task_url})",
            f"- Task ID: {task_id}",
            f"- Исполнитель: {assignee_name}",
            f"- Скорость работы: {assessment.speed}/5 — {assessment.speed_reason}",
            f"- Качество работы: {assessment.quality}/5 — {assessment.quality_reason}",
            f"- Плановое время: {self._format_minutes(assessment.planned_time_minutes)}",
            f"- Трекер времени: {self._format_minutes(assessment.tracked_time_minutes)}",
        ]
        if assessment.optimal_time_minutes is not None:
            entry_lines.append(
                f"- Оптимальное время (оценка ИИ): {self._format_minutes(assessment.optimal_time_minutes)}"
            )
        if assessment.time_estimate_realistic is not None:
            entry_lines.append(
                "- Плановое время реалистично: "
                f"{'да' if assessment.time_estimate_realistic else 'нет'}"
            )
        if assessment.context_adjustment is not None:
            entry_lines.append(
                f"- Контекстная корректировка: {assessment.context_adjustment:+.2f}"
            )
        if assessment.trend:
            entry_lines.append(
                f"- Тренд исполнителя: {self._translate_trend(assessment.trend)}"
            )
        if assessment.performer_level_match is not None:
            entry_lines.append(
                "- Соответствие уровню: "
                f"{'да' if assessment.performer_level_match else 'нет'}"
            )
        entry_lines.append(f"_Оценено: {timestamp}_")
        entry_lines.append("---")
        entry = "\n".join(entry_lines)
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            needs_leading_newline = self.history_path.exists() and self.history_path.stat().st_size > 0
            with self.history_path.open("a", encoding="utf-8") as handle:
                if needs_leading_newline:
                    handle.write("\n")
                handle.write(entry)
        except OSError as exc:
            logging.warning("Не удалось записать историю оценок: %s", exc)
        else:
            self._prune_history_file()

    def _close_if_needed(self, task: Dict[str, Any]) -> None:
        if not self._should_close(task):
            return
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            logging.warning("Пропуск закрытия задачи: отсутствует идентификатор.")
            return
        closed_status = self.config.closed_status or ""
        if self.dry_run:
            logging.info(
                "DRY RUN: задача %s была бы переведена в статус '%s'",
                task_id,
                closed_status,
            )
            return
        try:
            self._close_task(task_id)
        except requests.RequestException as exc:  # noqa: BLE001
            logging.error(
                "Не удалось перевести задачу %s в статус '%s': %s",
                task_id,
                closed_status,
                exc,
            )
        else:
            logging.info("Задача %s переведена в статус '%s'", task_id, closed_status)

    def _call_lm_completion(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.config.lm_base_url.rstrip('/')}/v1/chat/completions"
        logging.debug("Отправка запроса в LM Studio: %s", url)
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.lm_session.post(url, json=payload, timeout=60)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                if attempt >= max_attempts:
                    logging.error("LM Studio не ответила после %s попыток", attempt)
                    raise
                wait_seconds = min(2**attempt, 10)
                logging.warning(
                    "Ошибка LM Studio (попытка %s/%s): %s. Повтор через %s с.",
                    attempt,
                    max_attempts,
                    exc,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
        return {}

    @staticmethod
    def _build_retry_session(headers: Optional[Dict[str, str]] = None) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            read=3,
            connect=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods={"GET", "POST", "PUT"},
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        if headers:
            session.headers.update(headers)
        return session

    def _should_close(self, task: Dict[str, Any]) -> bool:
        if not self.config.closed_status:
            return False

        current_status = (task.get("status") or {}).get("status", "").lower()
        triggers = self.config.normalized_auto_close_statuses
        if not triggers:
            return False
        if current_status not in triggers:
            return False

        # Дополнительное условие: автозакрывать только задачи, которые завершены более недели назад.
        done_raw = task.get("date_done") or task.get("date_closed")
        if not done_raw:
            return False

        try:
            done_ms = int(done_raw)
        except (TypeError, ValueError):
            return False

        done_dt = datetime.fromtimestamp(done_ms / 1000, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = (now - done_dt).days
        if age_days <= 7:
            logging.info(
                "Пропуск автозакрытия задачи %s: дата завершения %s, прошло %s дн. (<= 7)",
                task.get("id"),
                done_dt.isoformat(),
                age_days,
            )
            return False

        return True

    def _close_task(self, task_id: str) -> None:
        url = f"{CLICKUP_API_BASE}/task/{task_id}"
        payload = {"status": self.config.closed_status}
        response = self.session.put(url, json=payload, timeout=30)
        response.raise_for_status()

    def _build_task_context(
        self,
        task: Dict[str, Any],
        details: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, Dict[str, Optional[int]]]:
        task_id = str(task.get("id") or "").strip()
        resolved_details = (
            details if details is not None else (self._get_task_details(task_id) if task_id else None)
        )
        description = (
            (resolved_details or {}).get("description")
            or task.get("description")
            or ""
        ).strip()
        sections: List[str] = []
        if description:
            sections.append(f"Описание задачи: {description}")
        else:
            sections.append("Описание задачи отсутствует.")

        time_metrics = self._extract_time_metrics(resolved_details or task)
        if time_metrics:
            planned = self._format_minutes(time_metrics.get("planned_minutes"))
            tracked = self._format_minutes(time_metrics.get("tracked_minutes"))
            sections.append(
                "Временные показатели:\n"
                f"- Плановое время: {planned}\n"
                f"- Трекер времени: {tracked}"
            )

        parent_section = self._parent_task_context(task, resolved_details)
        if parent_section:
            sections.append(parent_section)

        return "\n\n".join(sections), time_metrics

    def _parent_task_context(self, task: Dict[str, Any], details: Optional[Dict[str, Any]]) -> str:
        parent_id_raw = (details or {}).get("parent") or task.get("parent")
        parent_id = str(parent_id_raw).strip() if parent_id_raw else ""
        if not parent_id:
            return ""

        parent = self._get_task_details(parent_id)
        if not parent:
            return f"Родительская задача {parent_id}: подробности получить не удалось."

        parent_name = (parent.get("name") or "").strip() or f"ID {parent_id}"
        status_data = parent.get("status") or {}
        parent_status = ""
        if isinstance(status_data, dict):
            parent_status = (status_data.get("status") or "").strip()
        parent_description = (parent.get("description") or "").strip()
        parent_url = parent.get("url") or f"https://app.clickup.com/t/{parent_id}"

        lines = [f"Родительская задача: {parent_name} (ID {parent_id})"]
        if parent_status:
            lines.append(f"Статус родительской задачи: {parent_status}")
        if parent_url:
            lines.append(f"Ссылка на родительскую задачу: {parent_url}")
        if parent_description:
            lines.append(f"Описание родительской задачи: {parent_description}")
        else:
            lines.append("Описание родительской задачи отсутствует.")

        return "\n".join(lines)

    @staticmethod
    def _extract_primary_assignee(task_data: Dict[str, Any]) -> Tuple[Optional[str], str, str]:
        assignee_id: Optional[str] = None
        assignee_name = "не указан"
        assignee_role = "не указана"
        assignees = task_data.get("assignees") or []
        if isinstance(assignees, list) and assignees:
            first_assignee = assignees[0]
            if isinstance(first_assignee, dict):
                raw_id = first_assignee.get("id")
                if raw_id not in (None, ""):
                    normalized_id = str(raw_id).strip()
                    assignee_id = normalized_id or None
                name_candidate = (
                    first_assignee.get("username")
                    or first_assignee.get("email")
                    or first_assignee.get("user")
                    or first_assignee.get("id")
                )
                if name_candidate not in (None, ""):
                    normalized_name = str(name_candidate).strip()
                    if normalized_name:
                        assignee_name = normalized_name
                role_candidate = first_assignee.get("role")
                if role_candidate not in (None, ""):
                    normalized_role = str(role_candidate).strip()
                    if normalized_role:
                        assignee_role = normalized_role
            elif isinstance(first_assignee, str):
                normalized = first_assignee.strip()
                if normalized:
                    assignee_id = normalized
                    assignee_name = normalized
        return assignee_id, assignee_name, assignee_role

    @staticmethod
    def _format_minutes(raw_minutes: Optional[int]) -> str:
        if raw_minutes is None:
            return "нет данных"
        hours, minutes = divmod(raw_minutes, 60)
        if hours and minutes:
            return f"{hours}ч {minutes}м"
        if hours:
            return f"{hours}ч"
        return f"{minutes}м"

    @staticmethod
    def _extract_time_metrics(task: Dict[str, Any]) -> Dict[str, Optional[int]]:
        planned = task.get("time_estimate")
        tracked = task.get("time_spent")
        metrics: Dict[str, Optional[int]] = {}
        if isinstance(planned, (int, float)) and planned > 0:
            metrics["planned_minutes"] = int(round(planned / 60000))
        if isinstance(tracked, (int, float)) and tracked > 0:
            metrics["tracked_minutes"] = int(round(tracked / 60000))
        return metrics

    @staticmethod
    def _parse_clickup_timestamp(value: Optional[Any]) -> Optional[int]:
        if value in (None, "", 0, "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            logging.debug("Невозможно преобразовать отметку времени ClickUp: %s", value)
            return None

    @staticmethod
    def _format_timestamp(value: Optional[Any]) -> str:
        timestamp = ClickUpAgent._parse_clickup_timestamp(value)
        if timestamp is None:
            return "не указан"
        dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        return dt.isoformat()

    @staticmethod
    def _normalize_score(value: Any) -> Optional[float]:
        if not isinstance(value, (int, float)):
            return None
        normalized = float(value)
        if normalized <= 0:
            return None
        if normalized > 5:
            normalized = max(1.0, min(5.0, normalized / 2.0))
        return max(1.0, min(5.0, normalized))

    def _calculate_metric_average(
        self, records: List[Dict[str, Any]], key: str
    ) -> Optional[float]:
        values: List[float] = []
        for record in records:
            normalized = self._normalize_score(record.get(key))
            if normalized is not None:
                values.append(normalized)
        if not values:
            return None
        return sum(values) / len(values)

    def _calculate_average_score(self, records: List[Dict[str, Any]]) -> Optional[float]:
        combined: List[float] = []
        for record in records:
            speed = self._normalize_score(record.get("speed"))
            quality = self._normalize_score(record.get("quality"))
            components = [value for value in (speed, quality) if value is not None]
            if components:
                combined.append(sum(components) / len(components))
        if not combined:
            return None
        return sum(combined) / len(combined)

    @staticmethod
    def _get_performer_category(avg_score: Optional[float]) -> str:
        if avg_score is None:
            return "нет данных"
        if avg_score >= 4.5:
            return "Эксперт"
        if avg_score >= 3.5:
            return "Профессионал"
        if avg_score >= 2.5:
            return "Развивающийся"
        return "Проблемный"

    @staticmethod
    def _parse_optional_int(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_optional_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "y", "1"}:
                return True
            if lowered in {"false", "no", "n", "0"}:
                return False
        if isinstance(value, (int, float)):
            if value == 1:
                return True
            if value == 0:
                return False
        return None

    def _compute_deadline_status(self, task: Dict[str, Any], due_raw: Optional[Any]) -> str:
        due_timestamp = self._parse_clickup_timestamp(due_raw)
        if due_timestamp is None:
            return "не указан"
        closed_raw = task.get("date_closed") or task.get("date_done")
        closed_timestamp = self._parse_clickup_timestamp(closed_raw)
        now_timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        if closed_timestamp is None:
            return "в срок" if due_timestamp >= now_timestamp else "просрочено"
        return "в срок" if closed_timestamp <= due_timestamp else "просрочено"

    @staticmethod
    def _translate_trend(trend: str) -> str:
        mapping = {
            "progress": "прогресс",
            "stable": "стабильно",
            "regression": "регресс",
        }
        return mapping.get(trend.lower(), trend)

    @staticmethod
    def _extract_json_payload(content: Any) -> str:
        if not isinstance(content, str):
            return ""
        stripped = content.strip()
        if not stripped:
            return ""

        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines:
                # drop opening fence line
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()

        return stripped

    @staticmethod
    def _truncate_words(value: str, max_words: int) -> str:
        words = value.split()
        if len(words) <= max_words:
            return value
        return " ".join(words[:max_words])

    def _prune_history_file(self) -> None:
        if self.history_limit <= 0:
            return
        if not self.history_path.exists():
            return
        try:
            raw_content = self.history_path.read_text(encoding="utf-8")
        except OSError as exc:
            logging.warning("Не удалось прочитать историю для обрезки: %s", exc)
            return
        entries = self._split_history_entries(raw_content)
        if len(entries) <= self.history_limit:
            return
        trimmed = entries[-self.history_limit :]
        new_content = "\n\n".join(entry.strip() for entry in trimmed if entry.strip())
        new_content = f"{new_content.rstrip()}\n"
        try:
            self.history_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            logging.warning("Не удалось обрезать историю оценок: %s", exc)

    @staticmethod
    def _split_history_entries(raw_content: str) -> List[str]:
        if not raw_content.strip():
            return []
        lines = raw_content.splitlines()
        entries: List[str] = []
        current: List[str] = []
        for line in lines:
            current.append(line)
            if line.strip() == "---":
                entry = "\n".join(current).strip()
                if entry:
                    entries.append(entry)
                current = []
        if current and any(line.strip() for line in current):
            entry = "\n".join(current).strip()
            if entry:
                entries.append(entry)
        return entries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Агент для оценки задач в ClickUp с помощью LM Studio."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не изменять задачи, только выводить предполагаемые действия.",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Лимит обработанных задач за один запуск. По умолчанию без ограничения.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    config = build_config()
    if args.max_tasks is not None:
        if args.max_tasks < 1:
            logging.warning(
                "Игнорирую значение --max-tasks=%s: ожидалось положительное число.",
                args.max_tasks,
            )
            config.max_tasks = None
        else:
            config.max_tasks = args.max_tasks
    agent = ClickUpAgent(config=config, dry_run=args.dry_run)
    agent.run()


if __name__ == "__main__":
    main()
