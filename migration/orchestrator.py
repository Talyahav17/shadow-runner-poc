"""Orchestrator: the main agent, which always runs on Sonnet 5.

Actor (migration/actor.py) still writes the candidate translation and
Critic (migration/critic.py) still reviews it against the empirical
evidence -- the orchestrator does neither. Its one job, run once per
iteration, is deciding which model ("brain") the Actor and Critic should
use for that iteration, based on the task at hand:

  - How structurally complex the target COBOL source looks (more
    branches/loops/intrinsic functions to translate correctly means a
    harder task, worth a more capable model from the start).
  - How the run has gone so far -- a REVISE verdict means the previous
    brain didn't get it right; the orchestrator escalates to a more
    capable model for the next attempt rather than retrying the same
    brain and hoping for a different answer.

This mirrors a real production router pattern: a fixed, always-on model
makes the cheap routing decision, and a heavier model is invoked only for
the sub-tasks that actually need it -- instead of paying for the biggest
model on every call regardless of difficulty, or under-provisioning a
genuinely hard translation with a model too small to get it right.

The routing decision itself is a real LLM call on ORCHESTRATOR_MODEL, not
a hardcoded lookup table -- but a deterministic heuristic
(decide_brains_heuristic) backs it up for --dry-run and for when the
orchestrator's own response fails to parse, so a routing hiccup degrades
to a safe default instead of crashing the whole migration run.
"""

import json
import re

from migration import llm_client

# Complexity signals pulled straight from the COBOL source text. None of
# these need a parse tree -- they're just a coarse proxy for "how much
# control flow does the Actor have to get exactly right".
_COMPLEXITY_PATTERNS = [
    r"\bPERFORM\b",
    r"\bIF\b",
    r"\bEVALUATE\b",
    r"\bFUNCTION\b",
    r"\bUNTIL\b",
]


def assess_complexity(cobol_source: str) -> int:
    """Coarse complexity score: total matches of the patterns above.
    Not a precise metric -- just enough signal to tell a one-line
    COMPUTE apart from a program with real branching/looping logic."""
    return sum(len(re.findall(pattern, cobol_source, re.IGNORECASE)) for pattern in _COMPLEXITY_PATTERNS)


def decide_brains_heuristic(iteration: int, history: list, complexity: int) -> dict:
    """Deterministic fallback/dry-run routing: no LLM call at all.

    - Complex source (score >= 4, e.g. a loop plus a couple of branches):
      start the Actor on Opus 5 rather than waiting for a REVISE to prove
      it's needed.
    - Any REVISE in the last iteration: escalate the Actor to Opus 5 for
      the next attempt -- repeating the same brain on the same failure
      wastes an iteration.
    - Two or more REVISEs in a row: escalate the Critic too, on the
      theory that the review itself may be missing something a fresh,
      more capable read of the evidence would catch.
    - Otherwise: both stay on Sonnet 5, the orchestrator's own tier --
      the default assumption for an as-yet-unproven-difficult task.
    """
    last_two_verdicts = [h["verdict"] for h in history[-2:]]
    revised_last = bool(history) and history[-1]["verdict"] == "REVISE"
    revised_twice = len(last_two_verdicts) == 2 and all(v == "REVISE" for v in last_two_verdicts)

    actor_model = llm_client.MODEL_SONNET_5
    critic_model = llm_client.MODEL_SONNET_5
    reasons = []

    if complexity >= 4:
        actor_model = llm_client.MODEL_OPUS_5
        reasons.append(f"complexity score {complexity} >= 4 -- starting Actor on Opus 5")
    if revised_last:
        actor_model = llm_client.MODEL_OPUS_5
        reasons.append("previous iteration was REVISE -- escalating Actor to Opus 5")
    if revised_twice:
        critic_model = llm_client.MODEL_OPUS_5
        reasons.append("two REVISEs in a row -- escalating Critic to Opus 5 for a fresh read")

    if not reasons:
        reasons.append(f"no escalation signal yet (iteration {iteration}, complexity {complexity}) -- default Sonnet 5 for both")

    return {"actor_model": actor_model, "critic_model": critic_model, "reasoning": "; ".join(reasons)}


ROUTING_PROMPT_TEMPLATE = """\
You are the orchestrator for an AI Actor/Critic COBOL-to-Python migration
pipeline. You do not translate code or review candidates yourself -- two
other agents do that. Your only job is deciding which Claude model each
of them should use for iteration {iteration}: "sonnet-5" (fast, capable,
the default) or "opus-5" (slower, more capable -- reserve it for genuinely
harder tasks or a run that's struggling).

COBOL source being migrated:

```cobol
{cobol_source}
```

Iteration history so far (empty if this is iteration 1):
{history_json}

Decide the model for this iteration's Actor (writes the candidate) and
Critic (reviews it against empirical evidence). Consider: does this
source look structurally complex (branches, loops, intrinsic functions)?
Has a previous attempt already failed (REVISE), suggesting the last brain
wasn't capable enough and a retry with the same one would likely repeat
the mistake?

Respond with ONLY a JSON object, no other text:
{{"actor_model": "sonnet-5" or "opus-5", "critic_model": "sonnet-5" or "opus-5", "reasoning": "<one sentence>"}}
"""

_MODEL_NAME_TO_ID = {
    "sonnet-5": llm_client.MODEL_SONNET_5,
    "opus-5": llm_client.MODEL_OPUS_5,
}


def build_routing_prompt(cobol_source: str, iteration: int, history: list) -> str:
    return ROUTING_PROMPT_TEMPLATE.format(
        cobol_source=cobol_source,
        iteration=iteration,
        history_json=json.dumps(history, indent=2) if history else "(none)",
    )


def _parse_routing_response(raw: str, fallback: dict) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return fallback
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return fallback

    actor_name = str(data.get("actor_model", "")).lower()
    critic_name = str(data.get("critic_model", "")).lower()
    if actor_name not in _MODEL_NAME_TO_ID or critic_name not in _MODEL_NAME_TO_ID:
        return fallback

    return {
        "actor_model": _MODEL_NAME_TO_ID[actor_name],
        "critic_model": _MODEL_NAME_TO_ID[critic_name],
        "reasoning": data.get("reasoning", ""),
    }


def decide_brains(orchestrate_llm, cobol_source: str, iteration: int, history: list) -> dict:
    """The real routing path: asks ORCHESTRATOR_MODEL (via orchestrate_llm,
    a call_llm closure already bound to that model) to pick a brain for
    the Actor and Critic this iteration. Falls back to the deterministic
    heuristic if the response doesn't parse -- a malformed routing
    response should degrade to a safe default, not crash the run."""
    complexity = assess_complexity(cobol_source)
    fallback = decide_brains_heuristic(iteration, history, complexity)
    prompt = build_routing_prompt(cobol_source, iteration, history)
    raw = orchestrate_llm(prompt)
    return _parse_routing_response(raw, fallback)


def make_stub_orchestrate_llm():
    """--dry-run stand-in for the orchestrator's own LLM call: no network
    access, deterministic. decide_brains still runs its real parsing path
    against this stub's output, so the plumbing is exercised the same way
    a real run would use it."""

    def stub(prompt: str) -> str:
        # Mirror the heuristic so a dry run's printed reasoning matches
        # what decide_brains_heuristic would have said anyway.
        history_match = re.search(r"Iteration history so far.*?:\n(.*?)\n\nDecide", prompt, re.DOTALL)
        history = []
        if history_match and history_match.group(1).strip() != "(none)":
            try:
                history = json.loads(history_match.group(1))
            except json.JSONDecodeError:
                history = []
        cobol_match = re.search(r"```cobol\n(.*?)```", prompt, re.DOTALL)
        cobol_source = cobol_match.group(1) if cobol_match else ""
        iteration_match = re.search(r"iteration (\d+)", prompt)
        iteration = int(iteration_match.group(1)) if iteration_match else 1

        complexity = assess_complexity(cobol_source)
        decision = decide_brains_heuristic(iteration, history, complexity)
        name_by_id = {v: k for k, v in _MODEL_NAME_TO_ID.items()}
        return json.dumps({
            "actor_model": name_by_id[decision["actor_model"]],
            "critic_model": name_by_id[decision["critic_model"]],
            "reasoning": decision["reasoning"],
        })

    return stub
