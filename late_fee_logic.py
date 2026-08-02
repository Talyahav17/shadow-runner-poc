"""Modern replacement for the legacy late_fee_calc.cbl COBOL routine.

Mirrors the legacy formula exactly:
    late_fee = balance * (penalty_rate / 100)

A second, deliberately differently-shaped program (different PIC field
widths than interest_calc.cbl) added specifically to prove the generic
proxy/registry architecture (cobol_parser.py, cobol_proxy.py,
program_registry.py) actually generalizes, rather than happening to work
for one program's exact shape.
"""

from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")
MAX_BALANCE = Decimal("99999.99")
MAX_PENALTY_RATE = Decimal("9.99")


def run_modern_logic(balance: float, penalty_rate: float) -> float:
    balance_dec = Decimal(str(balance))
    rate_dec = Decimal(str(penalty_rate))

    if balance_dec < 0 or rate_dec < 0:
        raise ValueError("balance and penalty_rate must be non-negative")
    if balance_dec > MAX_BALANCE:
        raise ValueError(f"balance exceeds PIC 9(5)V99 capacity ({MAX_BALANCE})")
    if rate_dec > MAX_PENALTY_RATE:
        raise ValueError(f"penalty_rate exceeds PIC 9V99 capacity ({MAX_PENALTY_RATE})")

    result = balance_dec * (rate_dec / Decimal("100"))
    result = result.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return float(result)
