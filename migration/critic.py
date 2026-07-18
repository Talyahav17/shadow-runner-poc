"""Critic: empirically validates a candidate, then asks the LLM for a
verdict grounded in that evidence.

The empirical comparison runs first and needs no LLM call at all -- it's
the same fuzzing approach as test_e2e_shadow.py, but calling the
candidate's Python function and the real compiled COBOL directly in
process rather than over HTTP. Only after that produces a match-rate and
concrete failures does the LLM get involved, and its job is to interpret
that evidence (root-cause the failures, decide if they're negligible
rounding noise or a real logic bug) -- not to guess correctness from
reading code alone.
"""

import json
import random
import re

FIXED_CASES = [
    (0.00, 0.00),
    (1000.00, 5.00),
    (250000.00, 3.75),
]


def run_empirical_comparison(
    candidate_fn,
    cobol_calculate,
    canonicalize,
    loan_dec_digits: int,
    rate_dec_digits: int,
    max_loan: float,
    max_rate: float,
    n: int = 300,
    seed: int = 42,
    tolerance: float = 0.001,
) -> dict:
    rng = random.Random(seed)
    cases = list(FIXED_CASES) + [(max_loan, max_rate), (0.01, max_rate)]
    for _ in range(n):
        precision = rng.randint(1, 6)
        loan = round(rng.uniform(0, max_loan), precision)
        rate = round(rng.uniform(0, max_rate), precision)
        cases.append((loan, rate))

    total = 0
    matches = 0
    failures = []
    for loan, rate in cases:
        canon_loan = canonicalize(loan, loan_dec_digits)
        canon_rate = canonicalize(rate, rate_dec_digits)
        try:
            expected = cobol_calculate(canon_loan, canon_rate)
        except Exception:
            # Ground truth itself rejected this input (out of range) --
            # not something the candidate can be judged against.
            continue
        total += 1
        try:
            actual = candidate_fn(canon_loan, canon_rate)
        except Exception as exc:
            failures.append({"loan": canon_loan, "rate": canon_rate, "error": repr(exc)})
            continue
        diff = abs(actual - expected)
        if diff <= tolerance:
            matches += 1
        else:
            failures.append({
                "loan": canon_loan, "rate": canon_rate,
                "expected": expected, "actual": actual, "diff": diff,
            })

    match_rate = (matches / total * 100) if total else 0.0
    return {
        "total": total,
        "matches": matches,
        "match_rate_pct": round(match_rate, 2),
        "failure_count": len(failures),
        "failures": failures[:20],
    }


VERDICT_PROMPT_TEMPLATE = """\
You are reviewing a candidate Python translation of a legacy COBOL
program as part of a Shadow Runner migration.

COBOL source:

```cobol
{cobol_source}
```

Candidate Python translation:

```python
{candidate_code}
```

The candidate was just run against the real compiled COBOL program across
{total} inputs. Empirical result:
- Matches: {matches}/{total} ({match_rate_pct}%)
- Failures: {failure_count}

Sample failures (up to 20):
{failures_json}

Based on this empirical evidence (not just reading the code), decide
whether this candidate is ready to trust in production. A 100% match rate
with zero failures should be approved. Any failures mean REVISE -- root-
cause them (rounding mode? precision handling? a logic error?) and give
the next attempt specific, actionable feedback tied to the failing cases
above.

Respond with ONLY a JSON object, no other text:
{{"verdict": "APPROVE" or "REVISE", "feedback": "<specific guidance for the next revision, or a brief note if approved>"}}
"""


def build_verdict_prompt(cobol_source: str, candidate_code: str, empirical_result: dict) -> str:
    return VERDICT_PROMPT_TEMPLATE.format(
        cobol_source=cobol_source,
        candidate_code=candidate_code,
        total=empirical_result["total"],
        matches=empirical_result["matches"],
        match_rate_pct=empirical_result["match_rate_pct"],
        failure_count=empirical_result["failure_count"],
        failures_json=json.dumps(empirical_result["failures"], indent=2) or "(none)",
    )


def _parse_verdict(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {"verdict": "REVISE", "feedback": f"Critic response had no parseable JSON: {raw[:500]}"}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"verdict": "REVISE", "feedback": f"Critic response was not valid JSON: {raw[:500]}"}
    verdict = str(data.get("verdict", "REVISE")).upper()
    if verdict not in ("APPROVE", "REVISE"):
        verdict = "REVISE"
    return {"verdict": verdict, "feedback": data.get("feedback", "")}


def get_verdict(call_llm, cobol_source: str, candidate_code: str, empirical_result: dict) -> dict:
    prompt = build_verdict_prompt(cobol_source, candidate_code, empirical_result)
    raw = call_llm(prompt)
    return _parse_verdict(raw)
