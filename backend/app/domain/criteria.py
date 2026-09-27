import re
from typing import Any


def assess_criteria(
    score: dict[str, Any], texts: list[str], criteria: dict[str, Any]
) -> dict[str, Any]:
    errors = []
    if score["total"] < criteria["min_total"]:
        errors.append("Итоговый балл ниже установленного порога.")
    if len(score["violations"]) > criteria["max_errors"]:
        errors.append("Превышено допустимое количество нарушений.")
    spelling = sum(item["code"] == "SPELLING" for item in score["violations"])
    if spelling > criteria["max_spelling_errors"]:
        errors.append("Превышено допустимое количество орфографических ошибок.")
    for index, text in enumerate(texts):
        words = re.findall(r"[\w]+", text, re.UNICODE)
        if len(words) < criteria["min_words"]:
            errors.append(f"Ответ {index + 1}: недостаточно слов.")
        if criteria["sentence_end_required"] and not text.rstrip().endswith((".", "!", "?", "…")):
            errors.append(f"Ответ {index + 1}: нужен знак завершения предложения.")
    combined = {
        word.casefold().replace("ё", "е") for text in texts for word in re.findall(r"[\w]+", text)
    }
    missing = [
        term
        for term in criteria["required_terms"]
        if term.casefold().replace("ё", "е") not in combined
    ]
    if missing:
        errors.append("В ответах отсутствуют обязательные слова: " + ", ".join(missing))
    if not texts and (criteria["min_words"] or criteria["sentence_end_required"]):
        errors.append("Нет текстового ответа для проверки синтаксиса.")
    unavailable = []
    if "grammar" in score and not score["grammar"].get("available"):
        unavailable.append(
            "Порог орфографических ошибок не проверен: грамотность недоступна или отключена."
        )
    return {
        "passed": False if errors else None if unavailable else True,
        "rules": criteria,
        "errors": errors,
        "unavailable": unavailable,
        "version": "criteria-1",
    }
