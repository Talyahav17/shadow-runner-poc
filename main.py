"""Smart Proxy / Shadow Runner service.

Fronts the legacy COBOL interest_calc.so shared library. Every request is
served by the legacy COBOL routine (the "production" path, authoritative
and always returned to the caller). In parallel, a background task replays
the same inputs through the modern Python implementation (modern_logic.py)
and compares the two results, logging a success or a critical mismatch.
This lets the modern path be validated against real traffic with zero risk
to callers before it is ever promoted to production.
"""

import ctypes
import logging
import os
import secrets
import sys
import threading

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import shadow_store
from field_specs import (
    LOAN_INT_DIGITS,
    LOAN_DEC_DIGITS,
    RATE_INT_DIGITS,
    RATE_DEC_DIGITS,
    RESULT_INT_DIGITS,
    RESULT_DEC_DIGITS,
    MAX_LOAN_AMOUNT,
    MAX_INTEREST_RATE,
)
from modern_logic import run_modern_logic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("shadow_runner")

MISMATCH_TOLERANCE = 0.001

# Secure by default: if the operator doesn't pin SHADOW_RUNNER_API_KEY, a
# random key is generated for this run and logged once, so the service is
# never silently open. Set the env var to a fixed value for repeat access
# (e.g. from the client's own secrets manager).
API_KEY = os.environ.get("SHADOW_RUNNER_API_KEY")
if not API_KEY:
    API_KEY = secrets.token_urlsafe(24)
    logger.warning(
        "SHADOW_RUNNER_API_KEY not set -- generated a random key for this run. "
        "Set the env var to pin it across restarts. API key: %s", API_KEY,
    )


def require_api_key(x_api_key: str = Header(default=None, alias="X-API-Key")) -> None:
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


class CobolLibraryError(RuntimeError):
    pass


def _encode_fixed_point(value: float, int_digits: int, dec_digits: int) -> bytes:
    """Pack a float into COBOL DISPLAY (zoned decimal) format.

    A PIC 9(n)V9(m) DISPLAY field is stored as (n + m) ASCII digit bytes
    with no sign and no decimal point -- the point position is implied by
    the PIC clause, so encoding it is just scaling to an integer and
    zero-padding.
    """
    width = int_digits + dec_digits
    scaled = round(value * (10 ** dec_digits))
    max_value = 10 ** width - 1
    if scaled < 0 or scaled > max_value:
        raise ValueError(f"value {value} does not fit PIC 9({int_digits})V9({dec_digits})")
    return f"{scaled:0{width}d}".encode("ascii")


def _decode_fixed_point(raw: bytes, int_digits: int, dec_digits: int) -> float:
    """Inverse of _encode_fixed_point: ASCII digit bytes -> float."""
    width = int_digits + dec_digits
    digits = raw[:width].decode("ascii")
    scaled = int(digits)
    return scaled / (10 ** dec_digits)


def _canonicalize_to_field_precision(value: float, dec_digits: int) -> float:
    """Round a value down to what a PIC 9(n)V9(dec_digits) field can hold.

    The legacy COBOL field only has room for `dec_digits` fractional
    digits, so any extra precision in the caller's input is silently
    dropped the moment it's encoded for COBOL. The shadow (Python) path
    must see the exact same rounded value -- otherwise it "sees" precision
    the legacy system never had, and disagrees with production on inputs
    that were never actually equal once encoded.
    """
    scale = 10 ** dec_digits
    return round(value * scale) / scale


class CobolInterestCalculator:
    """ctypes wrapper around the compiled interest_calc.so module.

    interest_calc.cbl declares PROGRAM-ID INTEREST-CALC without RECURSIVE,
    so GnuCOBOL gives it a single process-global WORKING-STORAGE instance
    shared by every call -- concurrent calls from FastAPI's threadpool
    crash the whole process (libcob detects the reentrant call and aborts:
    "recursive CALL from 'INTEREST-CALC' to 'INTEREST-CALC' which is NOT
    RECURSIVE"). A real migration usually can't recompile or touch the
    legacy binary at all, so the fix belongs here, not in the .cbl: a lock
    serializes every call into the library, trading COBOL-call throughput
    for correctness -- exactly the assumption you'd make fronting any
    opaque legacy system whose internal thread-safety is unknown.
    """

    def __init__(self, lib_path: str):
        if not os.path.exists(lib_path):
            raise CobolLibraryError(
                f"COBOL shared library not found at {lib_path}. "
                "Run: cobc -m -o interest_calc.so interest_calc.cbl"
            )
        self._lib = ctypes.CDLL(lib_path)
        # GnuCOBOL runtime must be initialized once before any module call.
        self._lib.cob_init(0, None)
        self._entry_point = self._lib.INTEREST__CALC
        self._entry_point.restype = ctypes.c_int
        self._lock = threading.Lock()

    def calculate(self, loan_amount: float, interest_rate: float) -> float:
        loan_buf = ctypes.create_string_buffer(
            _encode_fixed_point(loan_amount, LOAN_INT_DIGITS, LOAN_DEC_DIGITS),
            LOAN_INT_DIGITS + LOAN_DEC_DIGITS,
        )
        rate_buf = ctypes.create_string_buffer(
            _encode_fixed_point(interest_rate, RATE_INT_DIGITS, RATE_DEC_DIGITS),
            RATE_INT_DIGITS + RATE_DEC_DIGITS,
        )
        result_buf = ctypes.create_string_buffer(
            RESULT_INT_DIGITS + RESULT_DEC_DIGITS
        )

        with self._lock:
            ret_code = self._entry_point(loan_buf, rate_buf, result_buf)
            if ret_code != 0:
                raise CobolLibraryError(f"interest_calc.so returned non-zero status {ret_code}")
            return _decode_fixed_point(result_buf.raw, RESULT_INT_DIGITS, RESULT_DEC_DIGITS)


LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "interest_calc.so")
cobol_calculator = CobolInterestCalculator(LIB_PATH)
shadow_store.init_db()

app = FastAPI(title="Shadow Runner - Interest Calculation Proxy")


class InterestRequest(BaseModel):
    loan_amount: float = Field(..., ge=0, le=MAX_LOAN_AMOUNT)
    interest_rate: float = Field(..., ge=0, le=MAX_INTEREST_RATE)


class InterestResponse(BaseModel):
    result: float
    source: str = "cobol-legacy"


def _run_shadow_comparison(loan_amount: float, interest_rate: float, legacy_result: float) -> None:
    """Runs the modern Python path and compares it against the legacy result.

    Executed as a FastAPI BackgroundTask, i.e. strictly after the HTTP
    response carrying the legacy result has already been sent -- so a slow,
    failing, or mismatching shadow run can never affect the caller.
    """
    try:
        shadow_result = run_modern_logic(loan_amount, interest_rate)
    except Exception as exc:
        logger.critical(
            "[SHADOW ERROR] modern_logic raised an exception for "
            "loan_amount=%s interest_rate=%s",
            loan_amount, interest_rate, exc_info=True,
        )
        shadow_store.record_result(
            loan_amount, interest_rate, legacy_result, None, None,
            status="error", detail=repr(exc),
        )
        return

    diff = abs(shadow_result - legacy_result)
    if diff > MISMATCH_TOLERANCE:
        logger.critical(
            "[SHADOW MISMATCH] loan_amount=%s interest_rate=%s "
            "legacy(cobol)=%s shadow(python)=%s diff=%s (tolerance=%s)",
            loan_amount, interest_rate, legacy_result, shadow_result, diff, MISMATCH_TOLERANCE,
        )
        shadow_store.record_result(
            loan_amount, interest_rate, legacy_result, shadow_result, diff,
            status="mismatch",
        )
    else:
        logger.info(
            "[SHADOW SUCCESS] loan_amount=%s interest_rate=%s "
            "legacy(cobol)=%s shadow(python)=%s diff=%s",
            loan_amount, interest_rate, legacy_result, shadow_result, diff,
        )
        shadow_store.record_result(
            loan_amount, interest_rate, legacy_result, shadow_result, diff,
            status="success",
        )


@app.post("/calculate-interest", response_model=InterestResponse, dependencies=[Depends(require_api_key)])
def calculate_interest(request: InterestRequest, background_tasks: BackgroundTasks):
    # 0. Canonicalize to the legacy field precision *before* either engine
    #    runs, so both see identical inputs -- not the caller's raw floats,
    #    which may carry more precision than the COBOL PIC clauses hold.
    loan_amount = _canonicalize_to_field_precision(request.loan_amount, LOAN_DEC_DIGITS)
    interest_rate = _canonicalize_to_field_precision(request.interest_rate, RATE_DEC_DIGITS)

    # 1. Production path: legacy COBOL, synchronous, authoritative.
    try:
        legacy_result = cobol_calculator.calculate(loan_amount, interest_rate)
    except CobolLibraryError as exc:
        logger.critical("[COBOL ERROR] %s", exc)
        raise HTTPException(status_code=502, detail="Legacy calculation engine failure") from exc

    # 2. Shadow path: modern Python, deferred until after the response is sent.
    background_tasks.add_task(
        _run_shadow_comparison, loan_amount, interest_rate, legacy_result
    )

    # 3. Safe return: caller always gets the legacy result.
    return InterestResponse(result=legacy_result)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/shadow-stats", dependencies=[Depends(require_api_key)])
def shadow_stats():
    """Aggregate match-rate across every shadow comparison recorded so far."""
    return shadow_store.get_match_stats()


@app.get("/shadow-history", dependencies=[Depends(require_api_key)])
def shadow_history(limit: int = Query(default=50, ge=1, le=500)):
    """Most recent shadow comparisons, newest first."""
    return shadow_store.get_recent_results(limit=limit)


@app.get("/dashboard")
def dashboard():
    """Client-facing match-rate dashboard. Static shell; it prompts for the
    API key client-side and uses it to call /shadow-stats and
    /shadow-history, so the page itself carries no secret."""
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "dashboard.html"))
