"""Smart Proxy / Shadow Runner service.

Fronts one or more legacy COBOL shared libraries, as configured in
programs.yaml (program_registry.py). Every request is served by the
legacy COBOL routine (the "production" path, authoritative and always
returned to the caller). In parallel, a background task replays the same
inputs through that program's modern Python implementation and compares
the two results, logging a success or a critical mismatch. This lets the
modern path be validated against real traffic with zero risk to callers
before it is ever promoted to production.
"""

import logging
import math
import os
import sys
import time

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import alerting
import auth
import metrics
import migration_store
import program_registry
import shadow_store
from cobol_proxy import CobolLibraryError, canonicalize_to_field_precision

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("shadow_runner")

MISMATCH_TOLERANCE = 0.001

view_only = Depends(auth.require_role(auth.VIEWER))
can_operate = Depends(auth.require_role(auth.OPERATOR))


try:
    registry = program_registry.load_registry()
except program_registry.ProgramRegistrationError as exc:
    # Fail loudly at startup rather than serving with a program silently
    # missing -- a shadow-runner that can't prove what it's fronting isn't
    # safe to trust.
    logger.critical("[REGISTRY ERROR] %s", exc)
    raise

if "interest_calc" not in registry:
    raise RuntimeError("programs.yaml must register 'interest_calc' (used by the /calculate-interest alias)")

shadow_store.init_db()
migration_store.init_db()

app = FastAPI(title="Shadow Runner - Legacy COBOL Migration Proxy")


class ProgramCalculateRequest(BaseModel):
    inputs: dict


class CalculateResponse(BaseModel):
    result: float
    source: str = "cobol-legacy"


class InterestRequest(BaseModel):
    """Kept for backward compatibility with the original single-program
    /calculate-interest endpoint; new integrations should use
    POST /programs/interest_calc/calculate instead."""
    loan_amount: float = Field(..., ge=0)
    interest_rate: float = Field(..., ge=0)


def _run_shadow_comparison(program_name: str, inputs: dict, legacy_result: float) -> None:
    """Runs the modern Python path for `program_name` and compares it
    against the legacy result.

    Executed as a FastAPI BackgroundTask, i.e. strictly after the HTTP
    response carrying the legacy result has already been sent -- so a slow,
    failing, or mismatching shadow run can never affect the caller.
    """
    program = registry[program_name]
    try:
        # Positional, not **inputs: a modern function's own parameter
        # names (e.g. modern_logic.run_modern_logic's `loan`/`rate`) have
        # no obligation to match the friendly API field names in
        # programs.yaml's input_params -- only their order does.
        shadow_result = program.modern_fn(*(inputs[p] for p in program.input_params))
    except Exception as exc:
        logger.critical(
            "[SHADOW ERROR] program=%s modern function raised an exception for inputs=%s",
            program_name, inputs, exc_info=True,
        )
        shadow_store.record_result(
            program_name, inputs, legacy_result, None, None,
            status="error", detail=repr(exc),
        )
        alerting.send_alert(
            f":rotating_light: *Shadow Runner error* on `{program_name}`\n"
            f"inputs={inputs}\nmodern function raised: {exc!r}"
        )
        metrics.SHADOW_COMPARISONS.labels(program=program_name, status="error").inc()
        return

    diff = abs(shadow_result - legacy_result)
    if diff > MISMATCH_TOLERANCE:
        logger.critical(
            "[SHADOW MISMATCH] program=%s inputs=%s legacy(cobol)=%s shadow(python)=%s diff=%s (tolerance=%s)",
            program_name, inputs, legacy_result, shadow_result, diff, MISMATCH_TOLERANCE,
        )
        shadow_store.record_result(program_name, inputs, legacy_result, shadow_result, diff, status="mismatch")
        alerting.send_alert(
            f":rotating_light: *Shadow Runner mismatch* on `{program_name}`\n"
            f"inputs={inputs}\nlegacy(cobol)={legacy_result}  shadow(python)={shadow_result}  diff={diff}"
        )
        metrics.SHADOW_COMPARISONS.labels(program=program_name, status="mismatch").inc()
    else:
        logger.info(
            "[SHADOW SUCCESS] program=%s inputs=%s legacy(cobol)=%s shadow(python)=%s diff=%s",
            program_name, inputs, legacy_result, shadow_result, diff,
        )
        shadow_store.record_result(program_name, inputs, legacy_result, shadow_result, diff, status="success")
        metrics.SHADOW_COMPARISONS.labels(program=program_name, status="success").inc()


def _validate_and_canonicalize_inputs(program, raw_inputs: dict) -> dict:
    """Checks every expected input is present, numeric, within that
    field's sign and PIC capacity (negative values are only allowed for
    fields parsed as PIC S9(n), i.e. field.signed), then canonicalizes
    each to the
    field's own precision *before* either engine runs -- so both see
    identical inputs, not the caller's raw floats which may carry more
    precision than the COBOL PIC clause holds (see cobol_proxy.py's
    canonicalize_to_field_precision docstring)."""
    canonical = {}
    for param_name, field in zip(program.input_params, program.input_fields):
        if param_name not in raw_inputs:
            raise HTTPException(status_code=422, detail=f"Missing required input '{param_name}'")
        value = raw_inputs[param_name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise HTTPException(status_code=422, detail=f"'{param_name}' must be a number")
        max_value = float(field.max_value_str())
        if not field.signed and value < 0:
            raise HTTPException(status_code=422, detail=f"'{param_name}' must be non-negative")
        if abs(value) > max_value:
            raise HTTPException(status_code=422, detail=f"'{param_name}' exceeds field capacity ({max_value})")
        canonical[param_name] = canonicalize_to_field_precision(value, field.dec_digits)
    return canonical


@app.post("/programs/{program_name}/calculate", response_model=CalculateResponse, dependencies=[can_operate])
def calculate_program(program_name: str, request: ProgramCalculateRequest, background_tasks: BackgroundTasks):
    program = registry.get(program_name)
    if program is None:
        raise HTTPException(status_code=404, detail=f"Unknown program '{program_name}'")

    try:
        canonical_inputs = _validate_and_canonicalize_inputs(program, request.inputs)
    except HTTPException:
        metrics.CALCULATE_REQUESTS.labels(program=program_name, outcome="validation_error").inc()
        raise

    # 1. Production path: legacy COBOL, synchronous, authoritative.
    try:
        start = time.perf_counter()
        legacy_result = program.proxy.calculate(*(canonical_inputs[p] for p in program.input_params))
        metrics.COBOL_CALL_DURATION.labels(program=program_name).observe(time.perf_counter() - start)
    except CobolLibraryError as exc:
        logger.critical("[COBOL ERROR] program=%s: %s", program_name, exc)
        metrics.CALCULATE_REQUESTS.labels(program=program_name, outcome="cobol_error").inc()
        raise HTTPException(status_code=502, detail="Legacy calculation engine failure") from exc

    metrics.CALCULATE_REQUESTS.labels(program=program_name, outcome="success").inc()

    # 2. Shadow path: modern Python, deferred until after the response is sent.
    background_tasks.add_task(_run_shadow_comparison, program_name, canonical_inputs, legacy_result)

    # 3. Safe return: caller always gets the legacy result.
    return CalculateResponse(result=legacy_result)


@app.post("/calculate-interest", response_model=CalculateResponse, dependencies=[can_operate])
def calculate_interest(request: InterestRequest, background_tasks: BackgroundTasks):
    """Backward-compatible alias for POST /programs/interest_calc/calculate."""
    return calculate_program(
        "interest_calc",
        ProgramCalculateRequest(inputs={"loan_amount": request.loan_amount, "interest_rate": request.interest_rate}),
        background_tasks,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics", dependencies=[view_only])
def metrics_endpoint():
    """Prometheus scrape endpoint -- same viewer-role auth as every other
    read endpoint, consistent with this service's RBAC rather than left
    open. Prometheus >=2.44 can send a custom header via a scrape config's
    `http_headers`; older Prometheus setups typically put a reverse proxy
    or sidecar in front that injects it instead."""
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


@app.get("/programs", dependencies=[view_only])
def list_programs():
    """Every legacy program this instance fronts, for dashboards/clients
    to discover dynamically instead of hardcoding program names."""
    return [
        {"name": p.name, "display_name": p.display_name, "input_params": p.input_params}
        for p in registry.values()
    ]


@app.get("/shadow-stats", dependencies=[view_only])
def shadow_stats(program: str = Query(default=None)):
    """Aggregate match-rate across shadow comparisons -- across every
    registered program by default, or filtered to one via ?program=."""
    return shadow_store.get_match_stats(program=program)


def _mask_value(v):
    """Rounds a number to 2 significant figures rather than redacting it
    entirely -- enough for a viewer-role caller to triage magnitude
    (roughly how big, roughly how far off) without seeing the exact
    financial figure."""
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v == 0:
        return v
    magnitude = 10 ** (math.floor(math.log10(abs(v))) - 1)
    return round(v / magnitude) * magnitude


@app.get("/shadow-history")
def shadow_history(
    limit: int = Query(default=50, ge=1, le=500),
    program: str = Query(default=None),
    role: str = Depends(auth.get_current_role),
):
    """Most recent shadow comparisons, newest first. Viewer-role callers
    get sensitive figures (inputs, legacy/shadow results, diff) masked to
    2 significant figures; operator/admin see exact values."""
    results = shadow_store.get_recent_results(limit=limit, program=program)
    if role == auth.VIEWER:
        for row in results:
            row["inputs"] = {k: _mask_value(v) for k, v in row["inputs"].items()}
            for key in ("legacy_result", "shadow_result", "diff"):
                row[key] = _mask_value(row[key])
            row["masked"] = True
    return results


@app.get("/dashboard")
def dashboard():
    """Client-facing match-rate dashboard. Static shell; it prompts for the
    API key client-side and uses it to call /shadow-stats and
    /shadow-history, so the page itself carries no secret."""
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "dashboard.html"))


@app.get("/migration-stats", dependencies=[view_only])
def migration_stats():
    """Progress across every legacy program registered with migrate.py:
    how many are pending/in_progress/approved/failed, plus per-program
    latest status and iteration count."""
    return migration_store.get_stats()


@app.get("/migration-history", dependencies=[view_only])
def migration_history(program: str, limit: int = Query(default=100, ge=1, le=500)):
    """Full iteration history for one registered program, most recent first."""
    return migration_store.get_program_history(program, limit=limit)


@app.get("/migration-dashboard")
def migration_dashboard():
    """Client-facing migration progress dashboard. Same pattern as
    /dashboard: static shell, prompts for the API key client-side."""
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "migration_dashboard.html"))


@app.get("/audit-log", dependencies=[view_only])
def audit_log(program: str = Query(default=None), limit: int = Query(default=200, ge=1, le=1000)):
    """Every migration approval/rejection decision: who, when, and why.
    Approving or rejecting itself is deliberately CLI-only
    (approve_migration.py), not an HTTP endpoint -- a production sign-off
    shouldn't be as lightweight as a single authenticated API call."""
    return migration_store.get_audit_log(limit=limit, program=program)
