"""Bounded HTTP counters for the single-process MVP, in Prometheus text format."""

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.3, 0.5, 1.0, 2.0, 10.0)
counts: dict[tuple[str, str, int], int] = defaultdict(int)
duration_sum: dict[tuple[str, str], float] = defaultdict(float)
duration_count: dict[tuple[str, str], int] = defaultdict(int)
buckets: dict[tuple[str, str, float], int] = defaultdict(int)
started_at = datetime.now(UTC)
diagnostic_write_failures = 0


def record_request(method: str, route: str, status: int, seconds: float) -> None:
    method = (
        method
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
        else "OTHER"
    )
    counts[method, route, status] += 1
    duration_sum[method, route] += seconds
    duration_count[method, route] += 1
    for bound in BUCKETS:
        if seconds <= bound:
            buckets[method, route, bound] += 1


def request_summary() -> dict[str, Any]:
    total = sum(duration_count.values())
    return {
        "since": started_at.isoformat(),
        "requests": total,
        "server_errors": sum(count for (_, _, status), count in counts.items() if status >= 500),
        "mean_ms": round(sum(duration_sum.values()) * 1000 / total, 2) if total else None,
        "over_two_seconds": total
        - sum(count for (_, _, bound), count in buckets.items() if bound == 2.0),
        "journal_write_failures": diagnostic_write_failures,
    }


def render_metrics() -> str:
    lines = [
        "# HELP dispatcher_http_requests_total Completed HTTP requests.",
        "# TYPE dispatcher_http_requests_total counter",
        "# HELP dispatcher_http_duration_seconds HTTP response duration.",
        "# TYPE dispatcher_http_duration_seconds histogram",
    ]
    for (method, route, status), count in sorted(counts.items()):
        labels = f'method={json.dumps(method)},route={json.dumps(route)},status="{status}"'
        lines.append(f"dispatcher_http_requests_total{{{labels}}} {count}")
    for (method, route), count in sorted(duration_count.items()):
        labels = f"method={json.dumps(method)},route={json.dumps(route)}"
        for bound in BUCKETS:
            lines.append(
                f'dispatcher_http_duration_seconds_bucket{{{labels},le="{bound}"}} '
                f"{buckets[method, route, bound]}"
            )
        lines.append(f'dispatcher_http_duration_seconds_bucket{{{labels},le="+Inf"}} {count}')
        lines.append(f"dispatcher_http_duration_seconds_count{{{labels}}} {count}")
        lines.append(
            f"dispatcher_http_duration_seconds_sum{{{labels}}} {duration_sum[method, route]}"
        )
    return "\n".join(lines) + "\n"
