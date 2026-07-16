"""Modern replacement for the legacy interest_calc.cbl COBOL routine.

Mirrors the legacy formula exactly:
    result = loan_amount * (interest_rate / 100)

Uses decimal.Decimal throughout (never float) to avoid binary
floating-point rounding drift, and rounds HALF_UP to 2 places to match
COBOL's ROUNDED clause on a PIC 9(7)V99 field.
"""

from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")

# Matches COBOL PIC 9(7)V99 / PIC 9(2)V99 (unsigned, fixed digit counts)
MAX_LOAN_AMOUNT = Decimal("9999999.99")
MAX_INTEREST_RATE = Decimal("99.99")


def run_modern_logic(loan: float, rate: float) -> float:
    """Compute simple interest using Decimal arithmetic.

    Args:
        loan: Loan principal amount (e.g. 1000.00).
        rate: Interest rate as a percentage (e.g. 5.00 for 5%).

    Returns:
        The computed interest, rounded HALF_UP to 2 decimal places.

    Raises:
        ValueError: If loan or rate is negative, or exceeds the field
            widths the legacy COBOL PIC clauses can represent.
    """
    loan_dec = Decimal(str(loan))
    rate_dec = Decimal(str(rate))

    if loan_dec < 0 or rate_dec < 0:
        raise ValueError("loan_amount and interest_rate must be non-negative")
    if loan_dec > MAX_LOAN_AMOUNT:
        raise ValueError(f"loan_amount exceeds PIC 9(7)V99 capacity ({MAX_LOAN_AMOUNT})")
    if rate_dec > MAX_INTEREST_RATE:
        raise ValueError(f"interest_rate exceeds PIC 9(2)V99 capacity ({MAX_INTEREST_RATE})")

    result = loan_dec * (rate_dec / Decimal("100"))
    result = result.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    return float(result)
