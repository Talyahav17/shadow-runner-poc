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

from cobol_proxy import canonicalize_to_field_precision


def run_empirical_comparison(
    candidate_fn,
    cobol_calculate,
    input_fields: list,
    n: int = 300,
    seed: int = 42,
    tolerance: float = 0.001,
) -> dict:
    """Generic across any program's input signature: input_fields is the
    list of cobol_parser.FieldSpec for that program's inputs, in calling
    order. Both candidate_fn and cobol_calculate are called positionally
    with the same N values, in that same order.
    """
    rng = random.Random(seed)
    max_values = [float(f.max_value_str()) for f in input_fields]

    cases = [
        tuple(0.0 for _ in input_fields),           # all-zero edge case
        tuple(max_values),                          # max-out-every-field edge case
    ]
    for _ in range(n):
        precision = rng.randint(1, 6)
        cases.append(tuple(round(rng.uniform(0, mv), precision) for mv in max_values))

    total = 0
    matches = 0
    failures = []
    for raw_values in cases:
        values = tuple(
            canonicalize_to_field_precision(v, f.dec_digits) for v, f in zip(raw_values, input_fields)
        )
        try:
            expected = cobol_calculate(*values)
        except Exception:
            # Ground truth itself rejected this input (out of range) --
            # not something the candidate can be judged against.
            continue
        total += 1
        try:
            actual = candidate_fn(*values)
        except Exception as exc:
            failures.append({"inputs": list(values), "error": repr(exc)})
            continue
        diff = abs(actual - expected)
        if diff <= tolerance:
            matches += 1
        else:
            failures.append({"inputs": list(values), "expected": expected, "actual": actual, "diff": diff})

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
