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
import sys

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from modern_logic import run_modern_logic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("shadow_runner")

MISMATCH_TOLERANCE = 0.001

# --- COBOL PIC clause widths (DISPLAY / zoned-decimal, no COMP usage) ---
# LS-LOAN-AMOUNT   PIC 9(7)V99  -> 7 integer digits + 2 decimal digits
# LS-INTEREST-RATE PIC 9(2)V99  -> 2 integer digits + 2 decimal digits
# LS-RESULT        PIC 9(7)V99  -> 7 integer digits + 2 decimal digits
LOAN_INT_DIGITS, LOAN_DEC_DIGITS = 7, 2
RATE_INT_DIGITS, RATE_DEC_DIGITS = 2, 2
RESULT_INT_DIGITS, RESULT_DEC_DIGITS = 7, 2

MAX_LOAN_AMOUNT = 10 ** LOAN_INT_DIGITS - 1 + (10 ** LOAN_DEC_DIGITS - 1) / 10 ** LOAN_DEC_DIGITS
MAX_INTEREST_RATE = 10 ** RATE_INT_DIGITS - 1 + (10 ** RATE_DEC_DIGITS - 1) / 10 ** RATE_DEC_DIGITS


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


class CobolInterestCalculator:
    """ctypes wrapper around the compiled interest_calc.so module."""

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

        ret_code = self._entry_point(loan_buf, rate_buf, result_buf)
        if ret_code != 0:
            raise CobolLibraryError(f"interest_calc.so returned non-zero status {ret_code}")

        return _decode_fixed_point(result_buf.raw, RESULT_INT_DIGITS, RESULT_DEC_DIGITS)


LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "interest_calc.so")
cobol_calculator = CobolInterestCalculator(LIB_PATH)

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
    except Exception:
        logger.critical(
            "[SHADOW ERROR] modern_logic raised an exception for "
            "loan_amount=%s interest_rate=%s",
            loan_amount, interest_rate, exc_info=True,
        )
        return

    diff = abs(shadow_result - legacy_result)
    if diff > MISMATCH_TOLERANCE:
        logger.critical(
            "[SHADOW MISMATCH] loan_amount=%s interest_rate=%s "
            "legacy(cobol)=%s shadow(python)=%s diff=%s (tolerance=%s)",
            loan_amount, interest_rate, legacy_result, shadow_result, diff, MISMATCH_TOLERANCE,
        )
    else:
        logger.info(
            "[SHADOW SUCCESS] loan_amount=%s interest_rate=%s "
            "legacy(cobol)=%s shadow(python)=%s diff=%s",
            loan_amount, interest_rate, legacy_result, shadow_result, diff,
        )


@app.post("/calculate-interest", response_model=InterestResponse)
def calculate_interest(request: InterestRequest, background_tasks: BackgroundTasks):
    # 1. Production path: legacy COBOL, synchronous, authoritative.
    try:
        legacy_result = cobol_calculator.calculate(request.loan_amount, request.interest_rate)
    except CobolLibraryError as exc:
        logger.critical("[COBOL ERROR] %s", exc)
        raise HTTPException(status_code=502, detail="Legacy calculation engine failure") from exc

    # 2. Shadow path: modern Python, deferred until after the response is sent.
    background_tasks.add_task(
        _run_shadow_comparison, request.loan_amount, request.interest_rate, legacy_result
    )

    # 3. Safe return: caller always gets the legacy result.
    return InterestResponse(result=legacy_result)


@app.get("/health")
def health():
    return {"status": "ok"}
