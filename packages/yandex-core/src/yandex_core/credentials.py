"""The only module in the project that touches a secret.

Read order is environment, then system keychain, then a ``0600`` fallback file
under the config directory.  Nothing returns a secret except :func:`get_secret`;
everything else in the codebase passes the value straight to the wire and never
into an argument, a log record, or an error message.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import config_dir
from .errors import AuthError, CredentialNotFound

__all__ = [
    "KEYRING_SERVICE",
    "REDACTED",
    "get_secret",
    "store_secret",
    "delete_secret",
    "secret_env_var",
    "redact_mapping",
    "redact_secret",
]

KEYRING_SERVICE = "yandex-mcp"
REDACTED = "***redacted***"

#: Field names whose values are never allowed into a log record or a message.
SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "app_password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "credential",
    }
)

_SETUP_HINT = "Run `yandex-mcp setup {service}` to store one."


def secret_env_var(service: str, profile: str) -> str:
    """The environment variable consulted for this service and profile."""
    return f"YANDEX_MCP_{service.upper()}_{profile.upper()}_PASSWORD".replace("-", "_")


def _account(service: str, profile: str) -> str:
    return f"{service}:{profile}"


def _fallback_path(service: str, profile: str) -> Path:
    return config_dir() / "credentials" / f"{service}.{profile}"


def get_secret(service: str, profile: str) -> str:
    """Return the stored app password for a service and profile.

    Raises:
        CredentialNotFound: if nothing is stored anywhere. The message points at
            setup and never contains a secret.
        AuthError: if something is stored but cannot be used -- a fallback file
            other users can read. That is a repair, not a setup step.
    """
    from_env = (os.environ.get(secret_env_var(service, profile)) or "").strip()
    if from_env:
        return from_env

    from_keyring = _keyring_get(service, profile)
    if from_keyring:
        return from_keyring

    from_file = _file_get(service, profile)
    if from_file:
        return from_file

    reason = f"No {service} app password stored for profile {profile!r}."
    raise CredentialNotFound(
        f"{reason} " + _SETUP_HINT.format(service=service), reason=reason
    )


def store_secret(service: str, profile: str, secret: str) -> str:
    """Store a secret, preferring the keychain and falling back to a 0600 file.

    Returns a human-readable description of where it landed -- never the value.
    """
    if not secret:
        raise AuthError("Refusing to store an empty app password.")
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, _account(service, profile), secret)
    except Exception:  # noqa: BLE001 - any keyring backend failure means fallback
        path = _fallback_path(service, profile)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(secret, encoding="utf-8")
        return f"{path} (mode 0600)"

    # A fallback file left over from an earlier store would outlive this one and
    # be served whenever the keychain happens to be locked, so it goes now.
    _fallback_path(service, profile).unlink(missing_ok=True)
    return "system keychain"


def delete_secret(service: str, profile: str) -> None:
    """Remove a stored secret from both the keychain and the fallback file.

    A secret that was never stored is not an error. A keyring backend that is
    present but fails is: reporting a deletion that did not happen would leave a
    live credential behind while the caller believes it is gone.

    Raises:
        Exception: whatever the keyring backend raised, other than a missing
            entry.
    """
    # The file goes first, so a keyring failure cannot leave it behind.
    _fallback_path(service, profile).unlink(missing_ok=True)
    try:
        import keyring
        import keyring.errors
    except ImportError:  # No keyring at all: the file was the only store.
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, _account(service, profile))
    except keyring.errors.PasswordDeleteError:
        # Nothing was stored under that key. Absence is the outcome we wanted.
        pass


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a mapping with secret-named fields replaced, recursively."""
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in SECRET_FIELD_NAMES:
            cleaned[key] = REDACTED
        elif isinstance(value, Mapping):
            cleaned[key] = redact_mapping(value)
        else:
            cleaned[key] = value
    return cleaned


def redact_secret(text: str, secret: str | None) -> str:
    """Replace a known secret wherever it appears in text.

    Nothing in this project puts a password into a message on purpose, but a
    library below us can: a transport error that quotes the URL it dialled will
    carry the credential if it was ever embedded there. Rendering an error
    verbatim is therefore not safe on its own.
    """
    if not secret:
        return text
    return text.replace(secret, REDACTED)


def _keyring_get(service: str, profile: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, _account(service, profile))
    except Exception:  # noqa: BLE001 - a missing or locked backend is not fatal
        return None


def _file_get(service: str, profile: str) -> str | None:
    path = _fallback_path(service, profile)
    if not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise AuthError(
            f"Credential file {path} is readable by others (mode {mode:04o}); "
            "run `chmod 600` on it before continuing."
        )
    return path.read_text(encoding="utf-8").strip() or None
