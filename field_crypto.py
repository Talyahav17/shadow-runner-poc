"""Optional field-level encryption for sensitive data at rest -- the
`inputs_json` column in shadow_store.py, i.e. real financial figures
(loan amounts, balances, rates) flowing through whatever programs are
registered.

Opt-in via SHADOW_RUNNER_ENCRYPTION_KEY (a Fernet key -- generate one
with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`,
or SHADOW_RUNNER_ENCRYPTION_KEY_FILE per secrets_helper.py).

Deliberately NOT auto-generated the way the API key is: an encryption key
has to stay IDENTICAL across restarts to keep previously-written data
readable, so silently rotating it on every restart (the API key's own
default behavior, which is fine there since callers just get told the
new one) would instead permanently lock this service out of its own
history. Unset by default -- data is stored in plaintext until this is
configured, with a one-time startup warning so that's a visible,
deliberate choice rather than a silent gap.
"""

import logging

import secrets_helper

logger = logging.getLogger("shadow_runner")

_KEY = secrets_helper.read_config("SHADOW_RUNNER_ENCRYPTION_KEY")
_fernet = None

if _KEY:
    from cryptography.fernet import Fernet
    _fernet = Fernet(_KEY.encode() if isinstance(_KEY, str) else _KEY)
else:
    logger.warning(
        "SHADOW_RUNNER_ENCRYPTION_KEY not set -- shadow comparison inputs are stored in "
        "plaintext. Set it to encrypt sensitive fields at rest (see field_crypto.py)."
    )


def is_enabled() -> bool:
    return _fernet is not None


def encrypt(plaintext: str) -> str:
    if not _fernet:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(value: str) -> str:
    """Falls back to returning the value unchanged if it isn't valid
    Fernet ciphertext -- e.g. rows written before encryption was enabled,
    or if it's later disabled again. A read path should degrade, not
    crash, when encryption state changes over a database's lifetime."""
    if not _fernet:
        return value
    from cryptography.fernet import InvalidToken
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return value
