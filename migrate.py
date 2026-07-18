#!/usr/bin/env python3
"""AI Actor/Critic migration pipeline for legacy COBOL programs.

Given a COBOL source file (already compiled to a shared library fronted
by main.py's CobolInterestCalculator), this drives an iterative loop:

  1. Actor (Claude) proposes a Python translation.
  2. The candidate is run against the REAL compiled COBOL across hundreds
     of fuzzed inputs -- no LLM involved in this step, just direct calls.
  3. Critic (Claude) reviews the candidate together with that empirical
     evidence and either APPROVEs it or REVISEs with feedback tied to the
     actual failing cases, which feeds back into step 1.

Stops once the Critic approves and the empirical match rate clears
--match-threshold, or --max-iterations is reached.

Requires ANTHROPIC_API_KEY for real runs (never entered interactively --
export it in your own shell). Use --dry-run to exercise the pipeline
mechanics with a stub LLM: no network calls, no cost, and it deliberately
returns a wrong-on-purpose first draft so you can watch a real revision
round happen.

SAFETY NOTE: this tool executes the LLM-generated candidate code locally
via exec() in order to test it -- that's the whole point (it must run to
be compared against the COBOL output), but it means you should only point
this at COBOL sources you're comfortable generating and running code for.
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# main.py logs a warning and generates a random API key at import time if
# SHADOW_RUNNER_API_KEY isn't set; this tool doesn't serve HTTP requests
# and doesn't need auth, so pin a throwaway value to keep output clean.
os.environ.setdefault("SHADOW_RUNNER_API_KEY", "migration-tool-internal-unused")

from migration import actor, critic, llm_client  # noqa: E402


def load_candidate_function(code: str):
    namespace = {}
    exec(code, namespace)  # noqa: S102 -- see module docstring SAFETY NOTE
    fn = namespace.get("run_modern_logic")
    if fn is None or not callable(fn):
        raise ValueError("candidate code did not define a callable run_modern_logic(loan, rate)")
    return fn


def make_stub_llm():
    """A deterministic fake LLM for --dry-run: exercises the full loop
    (including a revision round) without any network access or cost."""
    calls = {"n": 0}

    def stub(prompt: str) -> str:
        calls["n"] += 1
        if "Respond with ONLY a JSON object" in prompt:
            # This is a verdict request. Approve once the prompt shows the
            # (deliberately fixed) second-draft candidate was used.
            if "def run_modern_logic" in prompt and "* 1.0" not in prompt:
                return '{"verdict": "APPROVE", "feedback": "Matches the COBOL program on all tested inputs."}'
            return json.dumps({
                "verdict": "REVISE",
                "feedback": "The candidate applies an extra 1.0 multiplier that introduces drift. Remove it.",
            })
        # This is an Actor request (initial or revision).
        if "previous candidate" in prompt.lower() or "reviewer feedback" in prompt.lower() or "Fix the function" in prompt:
            # Second draft: correct.
            return '''```python
from decimal import Decimal, ROUND_HALF_UP

def run_modern_logic(loan: float, rate: float) -> float:
    loan_dec = Decimal(str(loan))
    rate_dec = Decimal(str(rate))
    if loan_dec < 0 or rate_dec < 0:
        raise ValueError("inputs must be non-negative")
    result = loan_dec * (rate_dec / Decimal("100"))
    return float(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
```'''
        # First draft: deliberately wrong (extra `* 1.0` sentinel the stub
        # verdict function looks for) so the loop is forced through a real
        # revision round during --dry-run.
        return '''```python
from decimal import Decimal, ROUND_HALF_UP

def run_modern_logic(loan: float, rate: float) -> float:
    loan_dec = Decimal(str(loan)) * 1.0
    rate_dec = Decimal(str(rate))
    if loan_dec < 0 or rate_dec < 0:
        raise ValueError("inputs must be non-negative")
    result = loan_dec * (rate_dec / Decimal("100"))
    return float(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
```'''

    return stub


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cobol", default="interest_calc.cbl", help="Path to the COBOL source to migrate")
    parser.add_argument("--model", default=llm_client.DEFAULT_MODEL, help="Claude model for Actor and Critic")
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--match-threshold", type=float, default=100.0, help="Minimum empirical match %% to accept an APPROVE verdict")
    parser.add_argument("--fuzz-cases", type=int, default=300, help="Number of random inputs per empirical comparison")
    parser.add_argument("--output-dir", default="migration_output")
    parser.add_argument("--dry-run", action="store_true", help="Use a stub LLM -- no API calls, no cost, exercises the pipeline mechanics only")
    args = parser.parse_args()

    cobol_path = Path(args.cobol)
    if not cobol_path.exists():
        print(f"COBOL source not found: {cobol_path}", file=sys.stderr)
        return 1
    cobol_source = cobol_path.read_text()

    import main as shadow_main  # local import: needs interest_calc.so already compiled

    if args.dry_run:
        print("--dry-run: using a stub LLM. No API calls, no cost.\n")
        call_llm = make_stub_llm()
    else:
        client = llm_client.get_client()
        call_llm = lambda prompt: llm_client.call_llm(client, prompt, model=args.model)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    candidate_code = None
    critic_feedback = None
    failures = None
    history = []
    approved = False

    for iteration in range(1, args.max_iterations + 1):
        print(f"=== Iteration {iteration}/{args.max_iterations} ===")
        print("Actor: proposing candidate...")
        candidate_code = actor.propose_candidate(call_llm, cobol_source, candidate_code, critic_feedback, failures)

        candidate_path = output_dir / f"candidate_v{iteration}.py"
        candidate_path.write_text(candidate_code)
        print(f"  wrote {candidate_path}")

        print("Running empirical comparison against the real compiled COBOL...")
        try:
            fn = load_candidate_function(candidate_code)
            empirical = critic.run_empirical_comparison(
                fn,
                shadow_main.cobol_calculator.calculate,
                shadow_main._canonicalize_to_field_precision,
                shadow_main.LOAN_DEC_DIGITS,
                shadow_main.RATE_DEC_DIGITS,
                shadow_main.MAX_LOAN_AMOUNT,
                shadow_main.MAX_INTEREST_RATE,
                n=args.fuzz_cases,
                seed=42 + iteration,
            )
        except Exception as exc:
            empirical = {"total": 0, "matches": 0, "match_rate_pct": 0.0, "failure_count": 1, "failures": [{"error": f"candidate failed to load: {exc!r}"}]}

        print(f"  match rate: {empirical['match_rate_pct']}% ({empirical['matches']}/{empirical['total']}), "
              f"failures: {empirical['failure_count']}")

        print("Critic: reviewing candidate + empirical evidence...")
        verdict = critic.get_verdict(call_llm, cobol_source, candidate_code, empirical)
        print(f"  verdict: {verdict['verdict']} -- {verdict['feedback']}")

        history.append({
            "iteration": iteration,
            "match_rate_pct": empirical["match_rate_pct"],
            "failure_count": empirical["failure_count"],
            "verdict": verdict["verdict"],
            "feedback": verdict["feedback"],
        })

        if verdict["verdict"] == "APPROVE" and empirical["match_rate_pct"] >= args.match_threshold:
            approved = True
            approved_path = output_dir / "approved_modern_logic.py"
            approved_path.write_text(candidate_code)
            print(f"\nAPPROVED after {iteration} iteration(s) -- wrote {approved_path}")
            break

        critic_feedback = verdict["feedback"]
        failures = empirical["failures"]
        print()

    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps({"cobol_source": str(cobol_path), "approved": approved, "iterations": history}, indent=2))
    print(f"\nFull iteration history written to {report_path}")

    if not approved:
        print(f"\nRESULT: DID NOT CONVERGE within {args.max_iterations} iteration(s).")
        return 1

    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
