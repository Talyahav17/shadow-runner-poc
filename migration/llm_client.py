"""Thin wrapper around the Anthropic SDK for the migration pipeline.

Kept separate from the actual prompts (actor.py / critic.py) so those can
be unit-tested against a stub `call_llm` callable without any network
access or API cost -- see migrate.py's --dry-run flag.
"""

import os

DEFAULT_MODEL = "claude-opus-4-8"


def get_client():
    """Raises RuntimeError with a clear message if ANTHROPIC_API_KEY isn't set,
    rather than letting the SDK's own (less obvious) error surface."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Export it in your own shell before "
            "running migrate.py -- this tool never asks for it interactively. "
            "Use --dry-run to exercise the pipeline mechanics without any API calls."
        )
    import anthropic  # imported lazily so --dry-run never requires the package
    return anthropic.Anthropic(api_key=api_key)


def call_llm(client, prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = 4096) -> str:
    """Sends a single-turn prompt and returns the concatenated text content."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
