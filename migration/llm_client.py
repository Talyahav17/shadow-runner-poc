"""Thin wrapper around the Anthropic SDK for the migration pipeline.

Kept separate from the actual prompts (actor.py / critic.py) so those can
be unit-tested against a stub `call_llm` callable without any network
access or API cost -- see migrate.py's --dry-run flag. `call_llm` itself
is also backend-agnostic: AnthropicBedrock and AnthropicVertex are
drop-in-compatible clients (same `.messages.create(...)` shape as the
plain Anthropic client), so nothing about the Actor/Critic prompt code
needs to know or care which backend is in use.

Three backends, selected via get_client(backend=...):

  anthropic (default) -- the public Anthropic API. Requires
    ANTHROPIC_API_KEY.

  bedrock -- Claude via AWS Bedrock, for enterprises whose data-residency
    requirements mean code/prompts can't leave their own AWS account.
    Requires `pip install "anthropic[bedrock]"` and AWS credentials
    configured the normal way (env vars, ~/.aws/credentials, or an IAM
    role) -- boto3's standard credential chain, since AnthropicBedrock
    delegates to it.

  vertex -- Claude via Google Cloud Vertex AI, same rationale for GCP.
    Requires `pip install "anthropic[vertex]"`, Application Default
    Credentials, and ANTHROPIC_VERTEX_PROJECT_ID (+ optionally
    ANTHROPIC_VERTEX_REGION, default us-central1).

HONESTY NOTE: bedrock/vertex were verified only as far as this
environment allows -- the client classes exist in the installed SDK,
their constructors accept the parameters below, and both construct
successfully with no live cloud credentials present. The actual API call
(an authenticated request reaching AWS/GCP) was NOT exercised here, since
this environment has no AWS or GCP account to test against. Confirm a
real call succeeds against your own account before relying on either.
"""

import secrets_helper

# Model IDs the pipeline knows how to route between. Kept as named
# constants (rather than scattering string literals across actor.py/
# critic.py/orchestrator.py) so there's one place to update when a new
# model generation ships.
MODEL_SONNET_5 = "claude-sonnet-5"
MODEL_OPUS_5 = "claude-opus-5"
MODEL_HAIKU_4_5 = "claude-haiku-4-5-20251001"

# The orchestrator (migration/orchestrator.py) always runs on this model --
# it's the "main agent" that decides which brain the Actor/Critic get per
# iteration, not a knob callers change per-run.
ORCHESTRATOR_MODEL = MODEL_SONNET_5

# Used when --model pins a single model for the whole run (dynamic
# per-iteration routing is skipped) and as the orchestrator's own
# fallback choice for Actor/Critic before it has any signal to route on.
DEFAULT_MODEL = MODEL_SONNET_5


def get_client(backend: str = "anthropic"):
    """backend is one of 'anthropic' (default), 'bedrock', 'vertex' --
    see module docstring for what each requires."""
    if backend == "anthropic":
        api_key = secrets_helper.read_config("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it in your own shell before "
                "running migrate.py -- this tool never asks for it interactively. "
                "Use --dry-run to exercise the pipeline mechanics without any API calls."
            )
        import anthropic  # imported lazily so --dry-run never requires the package
        return anthropic.Anthropic(api_key=api_key)

    if backend == "bedrock":
        try:
            import anthropic
            return anthropic.AnthropicBedrock(
                aws_region=secrets_helper.read_config("AWS_REGION", "us-east-1"),
            )
        except ImportError as exc:
            raise RuntimeError(
                'The bedrock backend needs boto3. Run: pip install "anthropic[bedrock]"'
            ) from exc

    if backend == "vertex":
        project_id = secrets_helper.read_config("ANTHROPIC_VERTEX_PROJECT_ID")
        if not project_id:
            raise RuntimeError("ANTHROPIC_VERTEX_PROJECT_ID is not set -- required for the vertex backend.")
        try:
            import anthropic
            return anthropic.AnthropicVertex(
                region=secrets_helper.read_config("ANTHROPIC_VERTEX_REGION", "us-central1"),
                project_id=project_id,
            )
        except ImportError as exc:
            raise RuntimeError(
                'The vertex backend needs google-auth. Run: pip install "anthropic[vertex]"'
            ) from exc

    raise ValueError(f"Unknown LLM backend '{backend}' -- must be 'anthropic', 'bedrock', or 'vertex'")


def call_llm(client, prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = 4096) -> str:
    """Sends a single-turn prompt and returns the concatenated text content.
    Identical across all three backends -- see module docstring."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
