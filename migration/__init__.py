"""AI Actor/Critic pipeline for migrating legacy COBOL programs to Python.

Mirrors the Actor -> Critic pattern used by the (separate, unrelated)
secure-auditor CLI in this same repo, but grounds the Critic's verdict in
real empirical evidence: the candidate is actually run against the
compiled legacy binary across hundreds of fuzzed inputs before the LLM
ever reviews it, so "approved" means "measured to match", not "looked
right on read-through".
"""
