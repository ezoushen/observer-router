"""Provider and chain schema: the TOML file, its validation and the defaults it fills.

Every value is type-checked, and messages name the provider and field but never echo a value: a
key pasted into the wrong field must not reach the log. A chain entry names a provider or a group;
a group rotates its members and cannot nest.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Per-type defaults. "openai" is any OpenAI-compatible endpoint (a local lane, a bridge), so it
# has no default URL and needs no key; the hosted types always authenticate.
_TYPE_DEFAULTS = {
    "gemini": {"url": GEMINI_URL, "budget_s": 18.0},
    "openrouter": {"url": OPENROUTER_URL, "budget_s": 24.0},
    "openai": {"url": "", "budget_s": 40.0},
}
# Field -> kind. Every value is type-checked at start-up: TOML "false" is a string, and a
# dataclass would store it and read it as true.
_COMMON_FIELDS = {
    "type": "str", "url": "url", "model": "str", "budget_s": "seconds",
    "api_key_env": "name", "api_key_file": "str", "api_key_var": "name",
    "buffer_stream": "bool",
    # Routing policy: served model ids whose reply fails over, and the least time worth calling for.
    "reject_models": "strs", "min_budget_s": "seconds",
    # Per-provider overrides of router-wide settings; unset, the router-wide value applies.
    "max_prompt_chars": "count", "break_after": "count", "break_seconds": "seconds",
}
_TYPE_FIELDS = {
    "gemini": {},
    "openrouter": {"site_url": "str", "app_name": "str"},
    # The GPU-lane gates only make sense for a lane on this machine, so only openai has them.
    "openai": {"reasoning_effort": "str", "chat_template_kwargs": "table",
               "serial": "bool", "ac_only": "bool",
               "idle_metrics": "urls", "idle_seconds": "seconds", "idle_signal": "table"},
}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ConfigError(Exception):
    """The config file is missing, unreadable, or describes an unusable chain."""


@dataclass(frozen=True)
class Provider:
    """One named upstream instance. Several instances may share a type."""

    name: str
    type: str
    url: str
    model: str
    budget: float
    api_key_env: str = ""
    api_key_file: str = ""
    api_key_var: str = ""
    site_url: str = ""
    app_name: str = "observer-router"
    reasoning_effort: str = ""
    chat_template_kwargs: dict | None = None
    serial: bool = False
    ac_only: bool = False
    buffer_stream: bool = False
    max_prompt_chars: int = 0  # 0: only the router-wide guard
    break_after: int = 0  # 0: OBSERVER_BREAK_AFTER
    break_seconds: float = 0.0  # 0: OBSERVER_BREAK_SECONDS
    idle_metrics: tuple[str, ...] = ()
    idle_seconds: float = 30.0
    idle_signal: dict | None = None  # which /metrics names mean busy; None: the Splash counters
    reject_models: tuple[str, ...] = ()  # served model ids whose reply counts as a failure
    min_budget: float = 1.0  # less of the request deadline left than this: not called


@dataclass(frozen=True)
class Config:
    providers: dict[str, Provider]
    # Entries name a provider or a group; a group rotates its members (see expand_chain).
    chain: tuple[str, ...]
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _http_url(value) -> bool:
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # noqa: B018 - reading it validates the port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _check_field(where: str, field: str, kind: str, value) -> None:
    """Raise ConfigError when value is not of kind. Messages never include the value: a key
    pasted into the wrong field must not be copied into the log."""
    valid = {
        "str": lambda v: isinstance(v, str),
        "name": lambda v: isinstance(v, str) and _NAME.fullmatch(v) is not None,
        "bool": lambda v: isinstance(v, bool),
        "count": lambda v: (isinstance(v, int) and not isinstance(v, bool)
                            and 1 <= v <= 1_000_000_000),
        "table": lambda v: isinstance(v, dict),
        "seconds": lambda v: (isinstance(v, (int, float)) and not isinstance(v, bool)
                              and math.isfinite(v) and v > 0),
        "url": lambda v: isinstance(v, str) and _http_url(v),
        # The idle sampler reads plain http only.
        "strs": lambda v: isinstance(v, list) and all(isinstance(s, str) and s for s in v),
        "urls": lambda v: isinstance(v, list) and all(
            isinstance(u, str) and _http_url(u) and u.startswith("http://") for u in v
        ),
    }[kind](value)
    if not valid:
        expected = {
            "str": "a string", "bool": "true or false", "count": "a whole number from 1", "table": "a table, e.g. {k = v}", "seconds": "a positive number",
            "name": "a variable name (letters, digits, underscore), not the key itself",
            "url": "an http(s) URL with a host", "urls": "a list of http:// URLs",
            "strs": "a list of non-empty strings",
        }[kind]
        raise ConfigError(f"{where}: {field} must be {expected}")


def _provider(name: str, table, min_retained_chars: int) -> Provider:
    where = f"providers.{name}"
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    kind = table.get("type")
    if kind not in _TYPE_DEFAULTS:
        raise ConfigError(f"{where}: type must be one of {', '.join(_TYPE_DEFAULTS)}")
    if "api_key" in table:
        # The value is never echoed: this message may land in a log.
        raise ConfigError(
            f"{where}: inline api_key is not allowed; use api_key_env or api_key_file"
        )
    kinds = {**_COMMON_FIELDS, **_TYPE_FIELDS[kind]}
    unknown = set(table) - set(kinds)
    if unknown:
        raise ConfigError(f"{where}: unknown field(s) for type {kind}: {', '.join(sorted(unknown))}")
    for key, value in table.items():
        _check_field(where, key, kinds[key], value)
    fields = {key: value for key, value in table.items()
              if key not in ("budget_s", "min_budget_s")}
    fields["url"] = table.get("url") or _TYPE_DEFAULTS[kind]["url"]
    if not fields["url"]:
        raise ConfigError(f"{where}: type openai needs a url")
    if not table.get("model"):
        raise ConfigError(f"{where}: model is required")
    if bool(table.get("api_key_file")) != bool(table.get("api_key_var")):
        raise ConfigError(f"{where}: api_key_file and api_key_var go together (the file, and the "
                          "key's name in it)")
    if kind != "openai" and not (table.get("api_key_env") or table.get("api_key_file")):
        raise ConfigError(f"{where}: type {kind} needs api_key_env or api_key_file")
    if "api_key_file" in fields:
        fields["api_key_file"] = os.path.expanduser(fields["api_key_file"])
    if "reject_models" in fields:
        fields["reject_models"] = tuple(fields["reject_models"])
    if "idle_metrics" in fields:
        fields["idle_metrics"] = tuple(str(url) for url in fields["idle_metrics"])
    if "idle_seconds" in fields:
        fields["idle_seconds"] = float(fields["idle_seconds"])
    if "break_seconds" in fields:
        fields["break_seconds"] = float(fields["break_seconds"])
    # The guard keeps a message only if it can keep MIN_RETAINED_CHARS of it, and a prompt needs
    # room for a system message and the request it carries.
    if "idle_signal" in fields:
        if not fields.get("idle_metrics"):
            raise ConfigError(f"{where}: idle_signal needs idle_metrics, the lanes it is read from")
        fields["idle_signal"] = _idle_signal(where, fields["idle_signal"])
    floor = max(2 * min_retained_chars, 1)
    if fields.get("max_prompt_chars", floor) < floor:
        raise ConfigError(f"{where}: max_prompt_chars must be at least {floor}, enough to keep "
                          "a system message and the request")
    budget = float(table.get("budget_s", _TYPE_DEFAULTS[kind]["budget_s"]))
    min_budget = float(table.get("min_budget_s", 1.0))
    if "min_budget_s" in table and min_budget > budget:
        raise ConfigError(f"{where}: min_budget_s must not exceed budget_s")
    return Provider(name=name, budget=budget, min_budget=min_budget, **fields)


_METRIC_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")


def _idle_signal(where: str, value: dict) -> dict:
    """Check an idle_signal table: counters {submitted, finished}, or gauges {in_flight}."""
    counters, gauges = {"submitted", "finished"}, {"in_flight"}
    unknown = set(value) - counters - gauges
    if unknown:
        raise ConfigError(f"{where}: idle_signal has unknown key(s): {', '.join(sorted(unknown))}")
    if ("in_flight" in value) == bool(set(value) & counters):
        raise ConfigError(f"{where}: idle_signal must set exactly one of in_flight, or "
                          "submitted with finished")
    if set(value) & counters and set(value) != counters:
        raise ConfigError(f"{where}: idle_signal counters need both submitted and finished")

    def names(key: str, listed) -> list[str]:
        items = [listed] if key == "submitted" else listed
        if not isinstance(items, list) or not items or not all(
                isinstance(item, str) and _METRIC_NAME.fullmatch(item) for item in items):
            kind = "a metric name" if key == "submitted" else "a non-empty list of metric names"
            raise ConfigError(f"{where}: idle_signal {key} must be {kind}")
        return items

    result: dict[str, Any] = {key: names(key, value[key]) for key in value}
    listed = [*result.get("finished", []), *result.get("in_flight", [])]
    if len(set(listed)) != len(listed):
        raise ConfigError(f"{where}: idle_signal names a metric more than once")
    if "submitted" in result:
        result["submitted"] = result["submitted"][0]
    return result


def parse_config(data: dict, min_retained_chars: int = 1_024) -> Config:
    unknown = set(data) - {"chain", "providers", "groups"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    tables = data.get("providers") or {}
    if not isinstance(tables, dict) or not tables:
        raise ConfigError("config defines no [providers.<name>] tables")
    providers = {name: _provider(name, table, min_retained_chars)
                 for name, table in tables.items()}
    _check_lane_signals(providers)
    groups = _groups(data.get("groups", {}), providers)
    chain = data.get("chain", list(providers))
    if not isinstance(chain, list) or not chain or not all(isinstance(n, str) for n in chain):
        raise ConfigError("chain must be a non-empty list of provider or group names")
    return Config(providers, _checked_chain(chain, providers, groups, "chain"), groups)


def _check_lane_signals(providers: dict) -> None:
    """One /metrics URL is sampled once, so every provider that watches it must name the same
    signal."""
    seen: dict[str, tuple[str, dict | None]] = {}
    for name, provider in providers.items():
        for url in provider.idle_metrics:
            other = seen.setdefault(url, (name, provider.idle_signal))
            if other[1] != provider.idle_signal:
                raise ConfigError(f"idle_metrics URL {url} is read two ways, by providers "
                                  f"{other[0]} and {name}: give both the same idle_signal")


def _names(where: str, names, providers: dict) -> tuple[str, ...]:
    unknown = [name for name in names if name not in providers]
    if unknown:
        raise ConfigError(f"{where} names undefined provider(s): {', '.join(unknown)}")
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ConfigError(f"{where} repeats provider(s): {', '.join(repeated)}")
    return tuple(names)


def _groups(tables, providers: dict) -> dict[str, tuple[str, ...]]:
    """Parse [groups.<name>] tables. Members are providers only; a group cannot nest."""
    if not isinstance(tables, dict):
        raise ConfigError("groups must be [groups.<name>] tables")
    groups = {}
    for name, table in tables.items():
        where = f"groups.{name}"
        if name in providers:
            raise ConfigError(f"{where}: the name is already a provider")
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        unknown = set(table) - {"members"}
        if unknown:
            raise ConfigError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")
        members = table.get("members")
        if not isinstance(members, list) or not members or not all(
            isinstance(member, str) for member in members
        ):
            raise ConfigError(f"{where}: members must be a non-empty list of provider names")
        groups[name] = _names(where, members, providers)
    return groups


def _checked_chain(names, providers: dict, groups: dict, source: str) -> tuple[str, ...]:
    unknown = [name for name in names if name not in providers and name not in groups]
    if unknown:
        raise ConfigError(
            f"{source} names undefined provider(s) or group(s): {', '.join(unknown)}"
        )
    expanded = [member for name in names for member in groups.get(name, (name,))]
    repeated = sorted({name for name in expanded if expanded.count(name) > 1})
    if repeated:
        raise ConfigError(
            f"{source} would try provider(s) more than once, directly or through a group: "
            f"{', '.join(repeated)}"
        )
    return tuple(names)


def load_config(path: str, min_retained_chars: int = 1_024) -> Config:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as error:
        raise ConfigError(f"cannot read config {path}: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from None
    return parse_config(data, min_retained_chars)


def chain_override(config: Config, text: str) -> tuple[str, ...]:
    """Apply OBSERVER_ROUTER_CHAIN: comma-separated provider or group names, or empty."""
    names = [name.strip() for name in text.split(",") if name.strip()]
    if not names:
        return config.chain
    return _checked_chain(names, config.providers, config.groups, "OBSERVER_ROUTER_CHAIN")


# Environment variables that configured providers before the config file existed. Setting one
# now does nothing, so start-up names them rather than ignoring them silently.
_REMOVED_ENV_PREFIXES = ("OBSERVER_LOCAL_", "OBSERVER_CURSOR_", "OBSERVER_GEMINI_",
                         "OBSERVER_OPENROUTER_")


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def provider_warnings(providers: dict, max_prompt_chars: int = 240_000) -> list[str]:
    """Settings that parse but never act, so /health must not be read as if they did."""
    warnings = [
        f"providers.{name}: max_prompt_chars {provider.max_prompt_chars} is above the router-wide "
        f"OBSERVER_MAX_PROMPT_CHARS {max_prompt_chars}, which already bounds every prompt first"
        for name, provider in providers.items() if provider.max_prompt_chars > max_prompt_chars
    ]
    for name, provider in providers.items():
        if provider.ac_only and not _is_loopback(urllib.parse.urlsplit(provider.url).hostname):
            warnings.append(
                f"providers.{name}: ac_only reads the battery of the machine running the "
                "router, but url names another host, so it cannot see that host's power"
            )
    return warnings


def stale_environment(env) -> list[str]:
    return sorted(
        name for name in env
        if name.startswith(_REMOVED_ENV_PREFIXES) or name == "CLAUDE_MEM_SETTINGS"
    )
