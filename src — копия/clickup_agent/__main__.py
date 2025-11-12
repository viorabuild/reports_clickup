"""Entry point for running the ClickUp GPT agent from the command line."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import List, Optional

from .clickup import ClickUpClient
from .config import Settings, get_settings
from .orchestrator import TaskOrchestrator
from .reports import DailyReportGenerator


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ClickUp GPT агент — анализ задач и рекомендации."
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Доступные команды")
    
    # Analyze command (existing functionality)
    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Анализ задач с помощью GPT"
    )
    analyze_parser.add_argument(
        "--status",
        action="append",
        help="Фильтр по статусу (можно указать несколько раз).",
    )
    analyze_parser.add_argument(
        "--assignee",
        help="Идентификатор исполнителя для фильтрации задач.",
    )
    analyze_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не записывать результаты обратно в ClickUp.",
    )
    
    # Report command (new functionality)
    report_parser = subparsers.add_parser(
        "report",
        help="Генерация ежедневных отчётов по сотрудникам"
    )
    report_parser.add_argument(
        "--date",
        help="Дата отчёта в формате YYYY-MM-DD (по умолчанию: сегодня)",
    )
    report_parser.add_argument(
        "--output",
        help="Путь к файлу для сохранения отчётов (по умолчанию: вывод в консоль)",
    )
    report_parser.add_argument(
        "--status",
        action="append",
        help="Фильтр по статусу задач (можно указать несколько раз).",
    )
    report_parser.add_argument(
        "--assignee",
        help="Идентификатор исполнителя для ограничения выборки.",
    )
    
    # Global arguments
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Уровень логирования (DEBUG, INFO, WARNING...).",
    )
    
    args = parser.parse_args(argv)
    
    # Default to analyze if no command specified (backward compatibility)
    if args.command is None:
        args.command = "analyze"
    
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)

    settings = get_settings()
    
    if args.command == "analyze":
        return run_analyze(args, settings)
    elif args.command == "report":
        return run_report(args, settings)
    else:
        logging.getLogger(__name__).error("Неизвестная команда: %s", args.command)
        return 1


def run_analyze(args: argparse.Namespace, settings: Settings) -> int:
    """Run task analysis with GPT."""
    if hasattr(args, "dry_run") and args.dry_run and not settings.dry_run:
        settings = settings.model_copy(update={"dry_run": True})

    orchestrator = TaskOrchestrator(settings=settings)

    results = orchestrator.run(
        statuses=args.status if hasattr(args, "status") else None,
        assignee=args.assignee if hasattr(args, "assignee") else None,
    )

    logging.getLogger(__name__).info(
        "Обработка завершена. Рекомендации получены для %d задач.", len(results)
    )
    return 0


def run_report(args: argparse.Namespace, settings: Settings) -> int:
    """Generate daily reports for all employees."""
    logger = logging.getLogger(__name__)
    
    # Parse target date
    target_date = None
    if hasattr(args, "date") and args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            logger.error("Неверный формат даты. Используйте YYYY-MM-DD")
            return 1
    
    # Generate reports
    with ClickUpClient(settings) as clickup_client:
        generator = DailyReportGenerator(clickup_client, settings)
        reports = generator.generate_reports(
            target_date=target_date,
            statuses=getattr(args, "status", None),
            assignee=getattr(args, "assignee", None),
        )
    
    if not reports:
        logger.warning("Отчёты не сгенерированы. Проверьте наличие задач.")
        return 0
    
    # Output reports
    output_file = getattr(args, "output", None)
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            for report in reports:
                f.write(report.to_markdown())
                f.write("\n\n" + "="*80 + "\n\n")
        logger.info("Отчёты сохранены в файл: %s", output_file)
    else:
        for report in reports:
            print(report.to_markdown())
            print("\n" + "="*80 + "\n")
    
    logger.info("Сгенерировано отчётов: %d", len(reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
