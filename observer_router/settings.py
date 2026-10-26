"""Router-wide settings read from the environment.

A value that is not a number, or out of range, is collected as an error naming its variable and
the knob keeps its default; start-up reports the errors and exits. An empty value means "unset",
as a launchd plist leaves a variable it does not want to set.

The defaults are complete, so the router starts with no environment and no installation.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Mapping

# A DNS name, not a pattern: an allowed host is matched literally.
_HOST_NAME = re.compile(
    r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)

DEFAULT_CONFIG_PATH = "~/.config/observer-router/config.toml"


@dataclass(frozen=True)
class Settings:
    """The router's process-wide configuration.

    Character counts bound a prompt; seconds are wall-clock intervals; `port` is the TCP port
    the HTTP surface binds and `allowed_hosts` the extra DNS names a request's Host header may
    carry. `config_path` is the TOML file start-up reads.
    """

    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    config_path: str
    break_after: int
    break_seconds: float
    max_prompt_chars: int
    max_field_chars: int
    min_retained_chars: int
    base64_blob_chars: int
    power_cache_seconds: float
    idle_sample_seconds: float
    group_strikes: int


def _bound(minimum: float, maximum: float) -> str:
    return f"at least {minimum}" if maximum == math.inf else f"{minimum} to {maximum}"


def _int(environ: Mapping[str, str], name: str, default: int, minimum: int, maximum: int,
         errors: list[str]) -> int:
    """Read one integer variable; append an error and keep the default when it is unusable."""
    text = environ.get(name, "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        errors.append(f"{name} must be an integer {_bound(minimum, maximum)}")
        return default
    if not minimum <= value <= maximum:
        errors.append(f"{name} must be an integer {_bound(minimum, maximum)}")
        return default
    return value


def _float(environ: Mapping[str, str], name: str, default: float, minimum: float,
           maximum: float, errors: list[str]) -> float:
    """Read one number; a value that is not finite counts as unreadable."""
    text = environ.get(name, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        errors.append(f"{name} must be a number {_bound(minimum, maximum)}")
        return default
    if not math.isfinite(value) or not minimum <= value <= maximum:
        errors.append(f"{name} must be a number {_bound(minimum, maximum)}")
        return default
    return value


def _hosts(environ: Mapping[str, str], name: str, errors: list[str]) -> tuple[str, ...]:
    """Read a comma-separated list of DNS names, lower-cased; one bad entry rejects the list."""
    names = tuple(part.strip().lower() for part in environ.get(name, "").split(",")
                  if part.strip())
    for entry in names:
        if not _HOST_NAME.fullmatch(entry):
            errors.append(f"{name} must be comma-separated DNS names")
            return ()
    return names


def build_settings(environ: Mapping[str, str]) -> tuple[Settings, tuple[str, ...]]:
    """Return the settings this environment describes, and every error found while reading it."""
    errors: list[str] = []
    config = environ.get("OBSERVER_ROUTER_CONFIG") or DEFAULT_CONFIG_PATH
    settings = Settings(
        host=environ.get("OBSERVER_ROUTER_HOST", "127.0.0.1"),
        port=_int(environ, "OBSERVER_ROUTER_PORT", 1244, 1, 65535, errors),
        allowed_hosts=_hosts(environ, "OBSERVER_ROUTER_ALLOWED_HOSTS", errors),
        config_path=os.path.expanduser(config),
        break_after=_int(environ, "OBSERVER_BREAK_AFTER", 3, 1, 1_000_000_000, errors),
        break_seconds=_float(environ, "OBSERVER_BREAK_SECONDS", 60.0, 1.0, math.inf,
               errors),
        max_prompt_chars=_int(environ, "OBSERVER_MAX_PROMPT_CHARS", 240_000, 1, 1_000_000_000,
                 errors),
        max_field_chars=_int(environ, "OBSERVER_MAX_FIELD_CHARS", 80_000, 1, 1_000_000_000, errors),
        min_retained_chars=_int(environ, "OBSERVER_MIN_RETAINED_CHARS", 1_024, 0, 1_000_000_000,
                 errors),
        base64_blob_chars=_int(environ, "OBSERVER_BASE64_BLOB_CHARS", 16_384, 1, 1_000_000_000,
                 errors),
        power_cache_seconds=_float(environ, "OBSERVER_POWER_CACHE_SECONDS", 30.0, 0.0, math.inf,
                   errors),
        idle_sample_seconds=_float(environ, "OBSERVER_IDLE_SAMPLE_SECONDS", 2.0, 0.1, 3600.0, errors),
        group_strikes=_int(environ, "OBSERVER_GROUP_STRIKES", 2, 1, 1_000_000_000, errors),
    )
    return settings, tuple(errors)
