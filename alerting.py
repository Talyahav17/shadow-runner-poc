"""Sends an alert on shadow-comparison mismatches/errors, so someone
actually gets notified instead of a CRITICAL log line sitting unread.

Configure via SHADOW_RUNNER_ALERT_WEBHOOK_URL -- any endpoint accepting
Slack's incoming-webhook JSON body ({"text": "..."}). Slack, Mattermost,
Google Chat (via an adapter), and most generic webhook receivers all
speak this format, so this isn't Slack-specific despite the shape.

Left unset, alerting is simply a no-op -- the existing CRITICAL log line
is still written either way (see main.py's _run_shadow_comparison).
"""

import logging

import requests

import secrets_helper

logger = logging.getLogger("shadow_runner")

# SHADOW_RUNNER_ALERT_WEBHOOK_URL_FILE also honored -- see secrets_helper.py.
WEBHOOK_URL = secrets_helper.read_config("SHADOW_RUNNER_ALERT_WEBHOOK_URL")
TIMEOUT_SECONDS = 5


def send_alert(text: str) -> bool:
    """Best-effort: called from a background task (after the HTTP
    response was already sent), so a failed or slow webhook delivery can
    never affect a caller. Returns True on success, False otherwise
    (logged, never raised).
    """
    if not WEBHOOK_URL:
        return False
    try:
        resp = requests.post(WEBHOOK_URL, json={"text": text}, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("[ALERT DELIVERY FAILED] could not deliver alert to webhook: %r", exc)
        return False
