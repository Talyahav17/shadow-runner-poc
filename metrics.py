"""Prometheus metrics for the Shadow Runner service.

Counters and a latency histogram only -- match-rate and error-rate are
derived in Grafana/PromQL from the counters (rate(success)/rate(total)),
which is the idiomatic Prometheus pattern, rather than pre-computing and
exposing a gauge here that could drift from what the counters actually say.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

CALCULATE_REQUESTS = Counter(
    "shadow_runner_calculate_requests_total",
    "Total /calculate requests handled, by program and outcome",
    ["program", "outcome"],  # outcome: success | cobol_error | validation_error
)

SHADOW_COMPARISONS = Counter(
    "shadow_runner_shadow_comparisons_total",
    "Total shadow comparisons run, by program and result",
    ["program", "status"],  # status: success | mismatch | error
)

COBOL_CALL_DURATION = Histogram(
    "shadow_runner_cobol_call_duration_seconds",
    "Latency of the legacy COBOL call, by program",
    ["program"],
)


def render() -> tuple:
    """Returns (body_bytes, content_type) for a GET /metrics response."""
    return generate_latest(), CONTENT_TYPE_LATEST
