from clickup_agent.models import GPTRecommendation


def test_markdown_formatting_creates_headers():
    recommendation = GPTRecommendation(
        complexity="средняя",
        risks=["Риск задержки", "Недостаток ресурсов"],
        recommendations=["Согласовать сроки", "Назначить ответственного"],
        optimizations=["Автоматизировать отчётность"],
    )

    markdown = recommendation.to_markdown()

    assert "### Оценка сложности" in markdown
    assert markdown.count("###") == 4
    assert "- Риск задержки" in markdown
    assert markdown.endswith("- Автоматизировать отчётность")
