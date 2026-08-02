#!/usr/bin/env python3
"""AI Actor/Critic migration pipeline for legacy COBOL programs.

Given any COBOL source file (already compiled to a shared library --
cobc -m -o <name>.so <name>.cbl), this drives an iterative loop:

  1. Actor (Claude) proposes a Python translation.
  2. The candidate is run against the REAL compiled COBOL across hundreds
     of fuzzed inputs -- no LLM involved in this step, just direct calls.
  3. Critic (Claude) reviews the candidate together with that empirical
     evidence and either APPROVEs it or REVISEs with feedback tied to the
     actual failing cases, which feeds back into step 1.

Stops once the Critic approves and the empirical match rate clears
--match-threshold, or --max-iterations is reached.

Generic across any program whose LINKAGE SECTION cobol_parser.py can
parse (unsigned DISPLAY numeric fields) -- it is NOT limited to
interest_calc.cbl. Field widths, the calling convention, and the
candidate function's parameter names are all derived from the target
COBOL source itself; adding a new program to migrate is just
`--cobol your_program.cbl`, no code changes.

Every run is recorded in migration_results.db (migration_store.py) --
the program's status (pending/in_progress/approved/failed) and each
iteration's match rate -- so progress across runs is queryable via
main.py's GET /migration-dashboard, not just visible in this terminal
output.

By default (no --model given), migration/orchestrator.py -- the main
agent, which always runs on Sonnet 5 -- decides which model the Actor and
Critic get on each iteration, based on the target COBOL's complexity and
how the run is going (a REVISE verdict escalates the next attempt to a
more capable model rather than repeating the same brain on the same
failure). Pass --model to pin one model for the whole run instead and
skip that routing.

Requires ANTHROPIC_API_KEY for real runs (never entered interactively --
export it in your own shell). Use --dry-run to exercise the pipeline
mechanics with a stub LLM: no network calls, no cost, and it deliberately
returns a wrong-on-purpose first draft so you can watch a real revision
round happen. (The stub is shaped for interest_calc.cbl's 2-input
signature -- --dry-run against a differently-shaped program will still
exercise the loop mechanics, but the stub's fixed candidate code won't
match that program's actual fields. Real runs work for any program. The
orchestrator's own routing decision is also stubbed out in --dry-run --
see migration/orchestrator.py's make_stub_orchestrate_llm.)

SAFETY NOTE: this tool executes the LLM-generated candidate code locally
via exec() in order to test it -- that's the whole point (it must run to
be compared against the COBOL output), but it means you should only point
this at COBOL sources you're comfortable generating and running code for.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# main.py (imported transitively by nothing here now, but migration_store
# and cobol_proxy have no such requirement) -- kept for parity in case a
# future addition needs it. This tool no longer imports main.py or
# programs.yaml at all: it builds its own proxy directly from --cobol, so
# migrating a brand-new program never requires registering it first.
import cobol_parser  # noqa: E402
import migration_store  # noqa: E402
from cobol_proxy import GenericCobolProxy  # noqa: E402
from migration import actor, critic, llm_client, orchestrator  # noqa: E402


def load_candidate_function(code: str):
    namespace = {}
    exec(code, namespace)  # noqa: S102 -- see module docstring SAFETY NOTE
    fn = namespace.get("run_modern_logic")
    if fn is None or not callable(fn):
        raise ValueError("candidate code did not define a callable run_modern_logic(...)")
    return fn


def friendly_param_name(cobol_field_name: str) -> str:
    """'LS-LOAN-AMOUNT' -> 'loan_amount': a readable Python parameter name
    derived from the COBOL field name, since the generated function should
    read naturally rather than using COBOL's LS- naming convention."""
    name = re.sub(r"^LS-", "", cobol_field_name, flags=re.IGNORECASE)
    return name.lower().replace("-", "_")


def make_stub_llm():
    """A deterministic fake LLM for --dry-run: exercises the full loop
    (including a revision round) without any network access or cost.
    Shaped for interest_calc.cbl's (loan, rate) signature specifically --
    see module docstring."""

    def stub(prompt: str) -> str:
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

def run_modern_logic(loan_amount: float, interest_rate: float) -> float:
    loan_dec = Decimal(str(loan_amount))
    rate_dec = Decimal(str(interest_rate))
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

def run_modern_logic(loan_amount: float, interest_rate: float) -> float:
    loan_dec = Decimal(str(loan_amount)) * 1.0
    rate_dec = Decimal(str(interest_rate))
    if loan_dec < 0 or rate_dec < 0:
        raise ValueError("inputs must be non-negative")
    result = loan_dec * (rate_dec / Decimal("100"))
    return float(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
```'''

    return stub


def run_migration_loop(args, cobol_source: str, make_call_llm, orchestrate_llm, output_dir: Path, proxy, input_fields: list, param_names: list, program_id: int, run_id: str, program_name: str):
    """Runs the Actor -> empirical test -> Critic loop. Returns (approved, history).

    make_call_llm(model) builds a call_llm closure bound to that model --
    the orchestrator (running on Sonnet 5, via orchestrate_llm) decides
    which model the Actor and Critic get each iteration, unless --model
    pinned a single model for the whole run (orchestrate_llm is None in
    that case and every iteration uses the pinned model for both roles).
    """
    candidate_code = None
    critic_feedback = None
    failures = None
    history = []
    approved = False

    for iteration in range(1, args.max_iterations + 1):
        print(f"=== Iteration {iteration}/{args.max_iterations} ===")

        if orchestrate_llm is None:
            actor_model = critic_model = args.model
        else:
            decision = orchestrator.decide_brains(orchestrate_llm, cobol_source, iteration, history)
            actor_model, critic_model = decision["actor_model"], decision["critic_model"]
            print(f"Orchestrator (Sonnet 5): actor={actor_model}  critic={critic_model} -- {decision['reasoning']}")

        call_llm_actor = make_call_llm(actor_model)
        call_llm_critic = make_call_llm(critic_model)

        print("Actor: proposing candidate...")
        candidate_code = actor.propose_candidate(call_llm_actor, cobol_source, param_names, candidate_code, critic_feedback, failures)

        candidate_path = output_dir / f"candidate_v{iteration}.py"
        candidate_path.write_text(candidate_code)
        print(f"  wrote {candidate_path}")

        print("Running empirical comparison against the real compiled COBOL...")
        try:
            fn = load_candidate_function(candidate_code)
            empirical = critic.run_empirical_comparison(
                fn, proxy.calculate, input_fields,
                n=args.fuzz_cases, seed=42 + iteration,
            )
        except Exception as exc:
            empirical = {
                "total": 0, "matches": 0, "match_rate_pct": 0.0,
                "failure_count": 1, "failures": [{"error": f"candidate failed to load: {exc!r}"}],
            }

        print(f"  match rate: {empirical['match_rate_pct']}% ({empirical['matches']}/{empirical['total']}), "
              f"failures: {empirical['failure_count']}")

        print("Critic: reviewing candidate + empirical evidence...")
        verdict = critic.get_verdict(call_llm_critic, cobol_source, candidate_code, empirical)
        print(f"  verdict: {verdict['verdict']} -- {verdict['feedback']}")

        history.append({
            "iteration": iteration,
            "actor_model": actor_model,
            "critic_model": critic_model,
            "match_rate_pct": empirical["match_rate_pct"],
            "failure_count": empirical["failure_count"],
            "verdict": verdict["verdict"],
            "feedback": verdict["feedback"],
        })
        migration_store.record_iteration(
            program_id, run_id, iteration,
            empirical["match_rate_pct"], empirical["total"], empirical["failure_count"],
            verdict["verdict"], verdict["feedback"],
        )

        if verdict["verdict"] == "APPROVE" and empirical["match_rate_pct"] >= args.match_threshold:
            approved = True
            approved_path = output_dir / "candidate_awaiting_approval.py"
            approved_path.write_text(candidate_code)
            print(f"\nCritic approved after {iteration} iteration(s) -- wrote {approved_path}")
            print("This is NOT yet cleared for production: a human must review the evidence")
            print(f"and run approve_migration.py --program {program_name} --approver <you> to sign off.")
            break

        critic_feedback = verdict["feedback"]
        failures = empirical["failures"]
        print()

    return approved, history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cobol", default="interest_calc.cbl", help="Path to the COBOL source to migrate")
    parser.add_argument("--shared-library", default=None, help="Path to the compiled .so (default: same name as --cobol with a .so extension)")
    parser.add_argument(
        "--model", default=None,
        help="Pin a single Claude model for Actor and Critic on every iteration, disabling dynamic "
             "routing. Omit this to let the orchestrator (always Sonnet 5) pick a model per iteration "
             "based on task complexity and how the run is going -- see migration/orchestrator.py.",
    )
    parser.add_argument(
        "--llm-backend", default="anthropic", choices=["anthropic", "bedrock", "vertex"],
        help="Which Claude backend to use -- bedrock/vertex are for data-residency requirements "
             "that keep prompts inside your own AWS/GCP account (see migration/llm_client.py)",
    )
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

    so_path = Path(args.shared_library) if args.shared_library else cobol_path.with_suffix(".so")
    if not so_path.exists():
        print(f"Compiled shared library not found: {so_path}\nRun: cobc -m -o {so_path} {cobol_path}", file=sys.stderr)
        return 1

    try:
        linkage_fields = cobol_parser.parse_linkage_fields(cobol_source)
        program_id_name = cobol_parser.parse_program_id(cobol_source)
        using_order = cobol_parser.parse_using_clause(cobol_source)
    except (ValueError, cobol_parser.UnsupportedFieldError) as exc:
        print(f"Could not parse {cobol_path}: {exc}", file=sys.stderr)
        return 1

    fields_by_name = {f.name: f for f in linkage_fields}
    input_fields = [fields_by_name[name] for name in using_order[:-1]]
    result_field = fields_by_name[using_order[-1]]
    param_names = [friendly_param_name(f.name) for f in input_fields]

    entry_symbol = cobol_parser.entry_point_symbol(program_id_name)
    proxy = GenericCobolProxy(str(so_path), entry_symbol, input_fields, result_field)

    print(f"Target program: {program_id_name}  inputs: {param_names}\n")

    if args.dry_run:
        print("--dry-run: using a stub LLM. No API calls, no cost.\n")
        stub_llm = make_stub_llm()
        make_call_llm = lambda model: stub_llm  # noqa: E731 -- the stub ignores which model was requested
        orchestrate_llm = None if args.model else orchestrator.make_stub_orchestrate_llm()
    else:
        client = llm_client.get_client(backend=args.llm_backend)
        make_call_llm = lambda model: (lambda prompt: llm_client.call_llm(client, prompt, model=model))  # noqa: E731
        orchestrate_llm = None if args.model else (lambda prompt: llm_client.call_llm(client, prompt, model=llm_client.ORCHESTRATOR_MODEL))

    if args.model:
        print(f"--model pinned: using {args.model} for Actor and Critic on every iteration (no dynamic routing).\n")
    else:
        print(f"No --model given: orchestrator ({llm_client.ORCHESTRATOR_MODEL}) will pick a brain per iteration.\n")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    migration_store.init_db()
    program_name = cobol_path.stem
    program_id = migration_store.register_program(program_name, str(cobol_path))
    run_id = migration_store.start_run(program_id)

    approved = False
    history = []
    try:
        approved, history = run_migration_loop(
            args, cobol_source, make_call_llm, orchestrate_llm, output_dir, proxy, input_fields, param_names, program_id, run_id, program_name,
        )
    finally:
        # Runs even if the loop raised, so a crash mid-run shows as
        # "failed" on the dashboard rather than stuck on "in_progress".
        migration_store.finish_run(program_id, approved)

    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps({"cobol_source": str(cobol_path), "critic_approved": approved, "iterations": history}, indent=2))
    print(f"\nFull iteration history written to {report_path}")

    if not approved:
        print(f"\nRESULT: DID NOT CONVERGE within {args.max_iterations} iteration(s).")
        return 1

    print(f"\nRESULT: Critic approved -- awaiting human sign-off via approve_migration.py --program {cobol_path.stem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
