"""Single source of truth for the legacy COBOL PIC clause widths.

interest_calc.cbl defines:
    LS-LOAN-AMOUNT    PIC 9(7)V99
    LS-INTEREST-RATE  PIC 9(2)V99
    LS-RESULT         PIC 9(7)V99

Both main.py (ctypes encoding + request validation) and modern_logic.py
(shadow-path validation) must agree on exactly what these field widths
permit -- if the two ever computed their own max values independently and
drifted apart, that's precisely the kind of silent divergence the Shadow
Runner pattern exists to catch. So both import from here instead.
"""

LOAN_INT_DIGITS, LOAN_DEC_DIGITS = 7, 2
RATE_INT_DIGITS, RATE_DEC_DIGITS = 2, 2
RESULT_INT_DIGITS, RESULT_DEC_DIGITS = 7, 2


def max_value_str(int_digits: int, dec_digits: int) -> str:
    """Largest value representable in PIC 9(int_digits)V9(dec_digits), as an exact decimal string."""
    return f"{'9' * int_digits}.{'9' * dec_digits}"


MAX_LOAN_AMOUNT_STR = max_value_str(LOAN_INT_DIGITS, LOAN_DEC_DIGITS)
MAX_INTEREST_RATE_STR = max_value_str(RATE_INT_DIGITS, RATE_DEC_DIGITS)

MAX_LOAN_AMOUNT = float(MAX_LOAN_AMOUNT_STR)
MAX_INTEREST_RATE = float(MAX_INTEREST_RATE_STR)
