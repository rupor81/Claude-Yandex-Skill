"""Profiles: which account a server talks to, chosen once at start-up.

Profiles live in ``~/.config/yandex-mcp/config.toml`` and are selected by the
``YANDEX_MCP_PROFILE`` environment variable, never per call.  Nothing here reads
a secret -- that is ``credentials`` and only ``credentials``.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .errors import NotConfigured, ProtocolError

__all__ = [
    "Profile",
    "config_dir",
    "config_path",
    "selected_profile_name",
    "load_profile",
    "write_profile",
]

DEFAULT_CALDAV_URL = "https://caldav.yandex.ru"
PROFILE_ENV_VAR = "YANDEX_MCP_PROFILE"
CONFIG_DIR_ENV_VAR = "YANDEX_MCP_CONFIG_DIR"
DEFAULT_PROFILE_NAME = "default"

#: A profile name has to survive being a bare TOML key and a filename fragment.
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_SETUP_HINT = "Run `yandex-mcp setup calendar` to create one."


class Profile(BaseModel):
    """One Yandex account this connector may be pointed at."""

    name: str
    login: str = Field(description="Yandex login or full email address.")
    caldav_url: str = DEFAULT_CALDAV_URL


def config_dir() -> Path:
    """The directory holding config and the credential fallback file."""
    override = os.environ.get(CONFIG_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".config" / "yandex-mcp"


def config_path() -> Path:
    return config_dir() / "config.toml"


def selected_profile_name() -> str:
    """The profile named by the environment, falling back to the file default."""
    from_env = os.environ.get(PROFILE_ENV_VAR)
    if from_env:
        return from_env
    document = _read_document()
    default = document.get("default_profile")
    if isinstance(default, str) and default:
        return default
    return DEFAULT_PROFILE_NAME


def load_profile(name: str | None = None) -> Profile:
    """Load one profile.

    Raises:
        NotConfigured: if nothing is set up yet -- no config file, or a file with
            no profiles in it. Callers treat this as "you have not done setup",
            which is not a failure.
        ProtocolError: if configuration exists but is broken: unreadable, not
            valid TOML, or missing the profile that was explicitly asked for.
            That is an error, not an empty machine.
    """
    path = config_path()
    if not path.exists():
        reason = f"No profile configuration at {path}."
        raise NotConfigured(f"{reason} {_SETUP_HINT}", reason=reason)

    document = _read_document()
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        reason = f"No profiles defined in {path}."
        raise NotConfigured(f"{reason} {_SETUP_HINT}", reason=reason)

    resolved = name or selected_profile_name()
    entry = profiles.get(resolved)
    if entry is None:
        available = ", ".join(sorted(profiles)) or "none"
        message = (
            f"Profile {resolved!r} is not defined in {path} (defined: {available})."
        )
        # Asking for a profile by name and not finding it is an operator error:
        # the file is set up, just not with that profile. Only a default nobody
        # named is the "nothing set up yet" case.
        if name is not None:
            raise ProtocolError(message)
        raise NotConfigured(f"{message} {_SETUP_HINT}", reason=message)
    if not isinstance(entry, dict):
        raise ProtocolError(f"Profile {resolved!r} in {path} is not a table.")

    # A stray `name` key in the table would collide with the name we resolved,
    # so the resolved name wins and the table never supplies it.
    fields = {key: value for key, value in entry.items() if key != "name"}
    if "login" not in fields:
        raise ProtocolError(f"Profile {resolved!r} in {path} has no `login`.")

    try:
        return Profile(name=resolved, **fields)
    except ValidationError as exc:
        raise ProtocolError(f"Profile {resolved!r} in {path} is incomplete: {exc}") from exc


def write_profile(profile: Profile, *, make_default: bool = True) -> Path:
    """Persist a profile into the config TOML, preserving everything else.

    This is a read-modify-write: top-level keys this module knows nothing about,
    and per-profile keys it knows nothing about, survive untouched.

    Raises:
        ProtocolError: if the profile name is not a plain identifier, or the
            document cannot be represented as TOML.
    """
    if not PROFILE_NAME_PATTERN.match(profile.name):
        raise ProtocolError(
            f"Profile name {profile.name!r} is not usable: names must match "
            f"{PROFILE_NAME_PATTERN.pattern}."
        )

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    document = _read_document()
    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    document["profiles"] = profiles

    existing = profiles.get(profile.name)
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry["login"] = profile.login
    entry["caldav_url"] = profile.caldav_url
    entry.pop("name", None)
    profiles[profile.name] = entry

    if make_default:
        document["default_profile"] = profile.name
    elif not isinstance(document.get("default_profile"), str):
        # With `make_default=False` and nothing already chosen, the file simply
        # states no default; `selected_profile_name` falls back on its own.
        document.pop("default_profile", None)

    path.write_text(_render_document(document), encoding="utf-8")
    return path


def _render_document(document: dict[str, Any]) -> str:
    """Serialise a config document back to TOML, escaping every string."""
    scalars = {k: v for k, v in document.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in document.items() if isinstance(v, dict)}

    lines = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in sorted(scalars.items())]
    if lines:
        lines.append("")
    for name in sorted(tables):
        lines.extend(_render_table([name], tables[name]))
    return "\n".join(lines)


def _render_table(path: list[str], table: dict[str, Any]) -> list[str]:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    nested = {k: v for k, v in table.items() if isinstance(v, dict)}

    lines: list[str] = []
    header = ".".join(_toml_key(part) for part in path)
    if scalars or not nested:
        lines.append(f"[{header}]")
        lines.extend(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in sorted(scalars.items()))
        lines.append("")
    for name in sorted(nested):
        lines.extend(_render_table([*path, name], nested[name]))
    return lines


def _toml_key(key: object) -> str:
    if not isinstance(key, str):
        raise ProtocolError(f"Config key {key!r} is not a string.")
    if PROFILE_NAME_PATTERN.match(key):
        return key
    return _toml_string(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int) or isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ProtocolError(f"Config value of type {type(value).__name__} cannot be written.")


_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_string(value: str) -> str:
    out = ['"']
    for char in value:
        escaped = _TOML_ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _read_document() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProtocolError(f"Could not read {path}: {exc}") from exc
