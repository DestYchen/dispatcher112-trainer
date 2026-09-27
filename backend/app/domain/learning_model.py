"""Small, versioned CPU neural predictor; regulatory scores are never modified."""

import hashlib
import json
import math
from collections import defaultdict
from statistics import mean
from typing import Any

AXES = ("timeliness", "correctness", "completeness")
FEATURES = [
    *("mean_" + axis for axis in AXES),
    *("last_" + axis for axis in AXES),
    "recent_difficulty",
    "next_difficulty",
    "next_card_entry",
    "history_size",
]
MODEL_SCHEMA = "dds-mlp-1"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def features(history: list[dict[str, Any]], difficulty: int, task_mode: str) -> list[float]:
    if (
        len(history) < 3
        or not 1 <= difficulty <= 10
        or task_mode not in {"CARD_ENTRY", "CARD_ACTIONS"}
    ):
        raise ValueError(
            "Для прогноза нужны три оценённых задания и корректные параметры следующего."
        )
    recent = history[-3:]
    return [
        *(mean(row["axes"][axis] for row in recent) / 100 for axis in AXES),
        *(recent[-1]["axes"][axis] / 100 for axis in AXES),
        mean(row["difficulty"] for row in recent) / 10,
        difficulty / 10,
        float(task_mode == "CARD_ENTRY"),
        min(20, len(history)) / 20,
    ]


def predict(artifact: dict[str, Any], values: list[float]) -> dict[str, float]:
    if artifact["schema"] != MODEL_SCHEMA or len(values) != len(FEATURES):
        raise ValueError("Неподдерживаемая версия признаков модели.")
    output = values
    for index, layer in enumerate(artifact["layers"]):
        output = [
            sum(value * weight for value, weight in zip(output, weights, strict=True)) + bias
            for weights, bias in zip(layer["weights"], layer["bias"], strict=True)
        ]
        if index < len(artifact["layers"]) - 1:
            output = [max(0.0, value) for value in output]
    if not all(math.isfinite(value) for value in output):
        raise ValueError("Модель вернула некорректные значения.")
    return {
        axis: round(min(100.0, max(0.0, value * 100)), 2)
        for axis, value in zip(AXES, output, strict=True)
    }


def train(records: list[dict[str, Any]]) -> dict[str, Any]:
    import warnings

    import numpy as np
    from sklearn import __version__
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.neural_network import MLPRegressor
    from threadpoolctl import threadpool_limits

    ordered = sorted(records, key=lambda row: (row["student_id"], row["closed_at"], row["id"]))
    students: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ordered:
        if any(not 0 <= float(row["axes"][axis]) <= 100 for axis in AXES):
            raise ValueError("Учебная выборка содержит некорректные оценки.")
        students[row["student_id"]].append(row)
    eligible = sorted((key for key, rows in students.items() if len(rows) >= 4), key=digest)
    if len(eligible) < 5:
        raise ValueError(
            "Нужны результаты минимум пяти учащихся, не менее четырёх заданий у каждого."
        )
    held_out = set(eligible[: max(1, len(eligible) // 5)])
    samples: list[dict[str, Any]] = []
    for student in eligible:
        rows = students[student]
        for index in range(3, len(rows)):
            target = rows[index]
            samples.append(
                {
                    "student": student,
                    "id": target["id"],
                    "x": features(rows[:index], target["difficulty"], target["task_mode"]),
                    "y": [target["axes"][axis] / 100 for axis in AXES],
                    "baseline": rows[index - 1]["axes"],
                    "test": student in held_out,
                }
            )
    training = [row for row in samples if not row["test"]]
    testing = [row for row in samples if row["test"]]
    if len(training) < 20 or len(testing) < 5:
        raise ValueError(
            "Недостаточно последовательностей: нужны 20 обучающих и 5 контрольных пар."
        )
    network = MLPRegressor(
        hidden_layer_sizes=(16, 8),
        activation="relu",
        solver="lbfgs",
        alpha=0.1,
        max_iter=1000,
        random_state=112,
        tol=1e-7,
    )
    with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ConvergenceWarning)
        network.fit(
            np.array([row["x"] for row in training]), np.array([row["y"] for row in training])
        )
    artifact: dict[str, Any] = {
        "schema": MODEL_SCHEMA,
        "features": FEATURES,
        "axes": list(AXES),
        "algorithm": "MLPRegressor(16,8)/relu/lbfgs",
        "seed": 112,
        "sklearn": __version__,
        "layers": [
            {"weights": weights.T.tolist(), "bias": bias.tolist()}
            for weights, bias in zip(network.coefs_, network.intercepts_, strict=True)
        ],
        "dataset_sha256": digest(ordered),
        "training_students": [digest(student) for student in eligible if student not in held_out],
        "test_students": [digest(student) for student in eligible if student in held_out],
        "training_count": len(training),
        "test_count": len(testing),
        "converged": not any(
            issubclass(warning.category, ConvergenceWarning) for warning in captured
        ),
        "trained_through": max(row["closed_at"] for row in ordered),
    }
    comparisons = []
    for row in testing:
        prediction = predict(artifact, row["x"])
        actual = {axis: round(value * 100, 2) for axis, value in zip(AXES, row["y"], strict=True)}
        comparisons.append(
            {
                "sample": digest(row["id"]),
                "actual": actual,
                "predicted": prediction,
                "baseline": row["baseline"],
            }
        )
    artifact["comparisons"] = comparisons
    artifact["mae"] = {
        axis: round(
            mean(abs(row["actual"][axis] - row["predicted"][axis]) for row in comparisons), 2
        )
        for axis in AXES
    }
    artifact["baseline_mae"] = {
        axis: round(
            mean(abs(row["actual"][axis] - row["baseline"][axis]) for row in comparisons), 2
        )
        for axis in AXES
    }
    artifact["mae_overall"] = round(mean(artifact["mae"].values()), 2)
    artifact["baseline_mae_overall"] = round(mean(artifact["baseline_mae"].values()), 2)
    artifact["beats_baseline"] = artifact["mae_overall"] < artifact["baseline_mae_overall"]
    # Frozen metadata and JSON weights are self-contained; inference never unpickles code.
    artifact["version"] = digest(artifact)
    return artifact
