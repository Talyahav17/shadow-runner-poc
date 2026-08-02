"""Modern replacement for the legacy gcd_calc.cbl COBOL routine.

Mirrors the legacy Euclidean-algorithm result exactly: the greatest
common divisor of two integers, adapted from COBOLEval's HumanEval/13
(greatest_common_divisor), MIT licensed:
https://github.com/zorse-project/COBOLEval

Third registered program, and the first sourced from a real external
COBOL dataset rather than self-authored -- added to prove the
parser/proxy generalize to signed PIC S9(n) fields and to code this
system never wrote itself.
"""

import math

MAX_MAGNITUDE = 10**10 - 1


def run_modern_logic(a: float, b: float) -> float:
    a_int, b_int = int(a), int(b)

    if a_int != a or b_int != b:
        raise ValueError("a and b must be whole numbers for PIC S9(10) fields")
    if abs(a_int) > MAX_MAGNITUDE or abs(b_int) > MAX_MAGNITUDE:
        raise ValueError(f"a and b must fit PIC S9(10) (magnitude <= {MAX_MAGNITUDE})")

    return float(math.gcd(a_int, b_int))
