"""API-key authentication with roles (RBAC) for the Shadow Runner service.

Three roles, each a strict superset of the one before:
  viewer   -- read-only: dashboards, stats, history, audit log
  operator -- viewer + can trigger production calculations (POST endpoints)
  admin    -- operator + reserved for future admin-only actions

Configure multiple keys via SHADOW_RUNNER_API_KEYS="key1:admin,key2:operator,key3:viewer".
For backward compatibility, a single SHADOW_RUNNER_API_KEY (no roles) is
still honored and treated as a single admin-role key. If neither is set,
a random admin key is generated and logged once, same as before --
the service is never silently open by default.

Either can also be supplied via SHADOW_RUNNER_API_KEYS_FILE /
SHADOW_RUNNER_API_KEY_FILE (a path to a file containing the value) instead
of the plain env var -- see secrets_helper.py for why that matters for
Vault/CSI-driver/Docker-secrets style deployments.
"""

import logging
import secrets

from fastapi import Header, HTTPException

import secrets_helper

logger = logging.getLogger("shadow_runner")

VIEWER = "viewer"
OPERATOR = "operator"
ADMIN = "admin"

_ROLE_RANK = {VIEWER: 0, OPERATOR: 1, ADMIN: 2}


def _load_api_keys() -> dict:
    """Returns {key: role}. See module docstring for the env var formats."""
    keys_env = secrets_helper.read_config("SHADOW_RUNNER_API_KEYS")
    if keys_env:
        keys = {}
        for entry in keys_env.split(","):
            entry = entry.strip()
            if not entry:
                continue
            key, _, role = entry.partition(":")
            key, role = key.strip(), role.strip() or VIEWER
            if role not in _ROLE_RANK:
                raise RuntimeError(
                    f"Invalid role '{role}' in SHADOW_RUNNER_API_KEYS -- must be one of "
                    f"{', '.join(_ROLE_RANK)}"
                )
            keys[key] = role
        if keys:
            return keys

    single_key = secrets_helper.read_config("SHADOW_RUNNER_API_KEY")
    if single_key:
        return {single_key: ADMIN}

    generated = secrets.token_urlsafe(24)
    logger.warning(
        "Neither SHADOW_RUNNER_API_KEYS nor SHADOW_RUNNER_API_KEY is set -- generated a "
        "random admin-role key for this run. Set SHADOW_RUNNER_API_KEYS=key1:admin,key2:viewer "
        "to pin multiple role-scoped keys. API key: %s", generated,
    )
    return {generated: ADMIN}


API_KEYS = _load_api_keys()


def _resolve_role(x_api_key: str) -> str:
    """Timing-safe lookup: checks every configured key with
    secrets.compare_digest rather than a dict membership test, so a
    request's timing doesn't leak how many keys are configured or which
    prefix of a key was wrong."""
    matched_role = None
    for key, role in API_KEYS.items():
        if secrets.compare_digest(x_api_key or "", key):
            matched_role = role
    return matched_role


def require_role(min_role: str):
    """FastAPI dependency factory: require_role(auth.OPERATOR) etc."""

    def dependency(x_api_key: str = Header(default=None, alias="X-API-Key")) -> None:
        if not x_api_key:
            raise HTTPException(status_code=401, detail="Missing X-API-Key header")
        role = _resolve_role(x_api_key)
        if role is None:
            raise HTTPException(status_code=401, detail="Invalid X-API-Key")
        if _ROLE_RANK[role] < _ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"This endpoint requires the '{min_role}' role or higher (your key has '{role}')",
            )

    return dependency


def get_current_role(x_api_key: str = Header(default=None, alias="X-API-Key")) -> str:
    """Like require_role(VIEWER), but returns the resolved role instead of
    just pass/fail -- for endpoints whose response shape depends on which
    role is asking (e.g. masking sensitive figures for viewer-role reads,
    see main.py's shadow_history)."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    role = _resolve_role(x_api_key)
    if role is None:
        raise HTTPException(status_code=401, detail="Invalid X-API-Key")
    return role
