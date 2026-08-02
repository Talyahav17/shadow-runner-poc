"""Reads a config value from either a plain env var or a mounted secret
file, via the widely-used FOO_FILE convention.

Vault Agent injector, the Vault CSI driver, Docker/Swarm secrets, and
Kubernetes Secrets mounted as files all write the value to a file on
disk rather than an env var -- an env var is visible to anything that
can read `docker inspect` or /proc/<pid>/environ for the process, in a
way a file with restrictive permissions is not. Supporting FOO_FILE
means this service works with any of those without code changes: point
FOO_FILE at wherever the secrets tool wrote the value.

Falls back to the plain env var if FOO_FILE isn't set, so nothing about
existing plain-env-var deployments (docker-compose, local dev) changes.
"""

import os


def read_config(name: str, default=None):
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        with open(file_path) as f:
            return f.read().strip()
    return os.environ.get(name, default)
