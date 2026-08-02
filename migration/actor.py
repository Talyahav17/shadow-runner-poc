"""Actor: proposes a Python translation of a legacy COBOL program.

The Actor never sees empirical results directly on its first attempt --
only the COBOL source. On revision rounds it's given the previous
candidate, the Critic's feedback, and concrete failing (inputs, expected,
actual) examples pulled straight from the empirical comparison, so each
revision is grounded in specific evidence rather than a vague "try again".

Generic across any registered program's input signature (not just
interest_calc's loan/rate) -- the function contract is built from
whatever parameter names cobol_parser.py derived for the target program.
"""

import re

INITIAL_PROMPT_TEMPLATE = """\
You are migrating a legacy COBOL program to Python as part of a Shadow
Runner migration: the Python code you write will be run in parallel with
the real compiled COBOL program on live traffic, and every output will be
compared. Your goal is exact numerical agreement, not just "close enough".

Here is the COBOL source:

```cobol
{cobol_source}
```

Write a single Python function with this exact signature:

    {contract}

Requirements:
- Replicate the COBOL program's arithmetic EXACTLY, including its
  rounding behavior (COBOL's COMPUTE ... ROUNDED clause rounds half up).
- Use decimal.Decimal for all arithmetic -- never plain float math -- to
  avoid binary floating-point drift that would cause spurious mismatches
  against the COBOL output.
- Validate inputs are non-negative and raise ValueError if they exceed
  what the COBOL program's PIC clauses can represent (infer the field
  widths from the PIC clauses in the source above).
- Return a plain float from the function (round the Decimal result to the
  COBOL result field's precision first).

Respond with ONLY the Python code (imports included). No explanation
before or after, no markdown commentary -- a code block is fine, but
nothing outside it.
"""

REVISION_PROMPT_TEMPLATE = """\
You previously translated this COBOL program to Python:

```cobol
{cobol_source}
```

Your candidate:

```python
{previous_code}
```

A reviewer ran your candidate against the real compiled COBOL program
across many inputs and found it disagrees in some cases. Reviewer
feedback:

{critic_feedback}

Concrete failing examples ({param_list}, expected result from the real
COBOL program, what your candidate returned instead):

{failure_examples}

Fix the function ({contract}) so it agrees with the COBOL program on
these cases and in general. Respond with ONLY the corrected Python code
(imports included) -- no explanation, no markdown commentary outside a
code block.
"""


def build_contract(param_names: list) -> str:
    args = ", ".join(f"{name}: float" for name in param_names)
    return f"run_modern_logic({args}) -> float"


def _format_failures(failures: list, param_names: list) -> str:
    if not failures:
        return "(none provided)"
    lines = []
    for f in failures[:10]:
        if "error" in f:
            lines.append(f"  - candidate raised an error: {f['error']}")
        else:
            inputs_str = ", ".join(f"{name}={value}" for name, value in zip(param_names, f["inputs"]))
            lines.append(
                f"  - {inputs_str} -> expected(cobol)={f['expected']}, actual(candidate)={f['actual']} "
                f"(diff={f['diff']})"
            )
    return "\n".join(lines)


def build_initial_prompt(cobol_source: str, param_names: list) -> str:
    return INITIAL_PROMPT_TEMPLATE.format(cobol_source=cobol_source, contract=build_contract(param_names))


def build_revision_prompt(cobol_source: str, previous_code: str, critic_feedback: str, failures: list, param_names: list) -> str:
    return REVISION_PROMPT_TEMPLATE.format(
        cobol_source=cobol_source,
        previous_code=previous_code,
        critic_feedback=critic_feedback or "(no specific feedback provided)",
        failure_examples=_format_failures(failures, param_names),
        param_list=", ".join(param_names),
        contract=build_contract(param_names),
    )


def extract_code(raw_response: str) -> str:
    """Strips markdown code fences if the model wrapped its answer in one."""
    match = re.search(r"```(?:python)?\s*\n(.*?)```", raw_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw_response.strip()


def propose_candidate(
    call_llm,
    cobol_source: str,
    param_names: list,
    previous_code: str = None,
    critic_feedback: str = None,
    failures: list = None,
) -> str:
    """Returns candidate Python source (as a string) defining
    run_modern_logic(<param_names>) -> float."""
    if previous_code is None:
        prompt = build_initial_prompt(cobol_source, param_names)
    else:
        prompt = build_revision_prompt(cobol_source, previous_code, critic_feedback, failures, param_names)
    raw = call_llm(prompt)
    return extract_code(raw)
