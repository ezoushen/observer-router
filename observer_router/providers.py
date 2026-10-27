"""Request rewriting per provider type: the payload, the key and the headers."""

from __future__ import annotations

import json
import os
import re

from observer_router.config import Provider
from observer_router.failure import TierFailure


def payload_for(provider: Provider, body: dict, reasoning_mode: str | None) -> dict:
    payload = {
        "model": provider.model,
        "messages": body.get("messages") or [],
        "stream": bool(body.get("stream")),
    }
    for key in ("temperature", "max_tokens", "top_p", "stop"):
        if body.get(key) is not None:
            payload[key] = body[key]
    if provider.reasoning_effort:
        # Splash honours reasoning_effort ("none" switches thinking off) and ignores
        # chat_template_kwargs entirely. Without it a reasoning model spends the whole
        # max_tokens budget thinking and returns content=None with finish_reason=length, which
        # the router then reports as "empty content". Unset for lanes that reject unknown
        # fields (mlx_lm does).
        payload["reasoning_effort"] = provider.reasoning_effort
    if provider.chat_template_kwargs:
        # For servers that ignore reasoning_effort but render the template themselves (mtplx,
        # vLLM): {enable_thinking = false} is what turns Qwen thinking off there.
        payload["chat_template_kwargs"] = provider.chat_template_kwargs
    if provider.type == "openrouter" and reasoning_mode == "disabled":
        # Without this, reasoning models burn the whole budget on reasoning and
        # return empty content.
        payload["reasoning"] = {"enabled": False}
    elif provider.type == "openrouter" and reasoning_mode == "exclude":
        # Some endpoints reject enabled=false with HTTP 400; keep reasoning on but
        # hide it from the response.
        payload["reasoning"] = {"exclude": True}
    return payload


_QUOTED_VALUE = re.compile(r"""(["'])(.*)\1\s*(?:#.*)?""")


def _key_from_file(provider: Provider, path: str, var: str) -> str:
    """Read one key from a dotenv-style file, or from a JSON object when the path ends .json.

    Reading the key where it already lives (a bridge's env file, claude-mem's settings) keeps
    one copy of the secret, so rotating it there cannot silently break the provider. The file
    is re-read on every call, so a rotation takes effect without a restart.
    """
    found = ""
    try:
        with open(path) as handle:
            if path.endswith(".json"):
                value = json.load(handle).get(var)  # top-level keys only
                return str(value).strip() if value else ""
            for line in handle:  # the last assignment wins, as when the file is sourced
                name, sep, value = line.strip().removeprefix("export ").partition("=")
                if not sep or name.strip() != var:
                    continue
                quoted = _QUOTED_VALUE.fullmatch(value.strip())
                found = quoted.group(2) if quoted else value.split(" #", 1)[0].strip()
    except OSError as error:
        raise TierFailure(f"{provider.name}: cannot read {path}: {error.strerror}") from None
    except (ValueError, AttributeError) as error:
        # The decoder's message is not echoed: it can quote the file's contents.
        raise TierFailure(
            f"{provider.name}: cannot parse {path}: {type(error).__name__}"
        ) from None
    return found


def api_key(provider: Provider) -> str:
    """Return the provider's key: api_key_env first, then api_key_var in api_key_file."""
    if provider.api_key_env:
        key = os.environ.get(provider.api_key_env, "").strip()
        if key:
            return key
    if provider.api_key_file:
        return _key_from_file(provider, provider.api_key_file, provider.api_key_var)
    return ""


def headers_for(provider: Provider) -> dict:
    headers = {"Content-Type": "application/json"}
    wants_key = provider.api_key_env or provider.api_key_file
    if wants_key:
        key = api_key(provider)
        if not key:
            sources = " or ".join(filter(None, (
                provider.api_key_env and f"${provider.api_key_env}",
                provider.api_key_file and f"{provider.api_key_var} in {provider.api_key_file}",
            )))
            raise TierFailure(f"{provider.name}: no API key in {sources}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in key):
            # http.client would reject the header and quote the whole key in its error.
            raise TierFailure(f"{provider.name}: API key contains control characters")
        headers["Authorization"] = f"Bearer {key}"
    if provider.type == "openrouter":
        if provider.site_url:
            headers["HTTP-Referer"] = provider.site_url
        headers["X-Title"] = provider.app_name
    return headers


# Upstream traffic goes through http.client directly: only the HTTPS and HTTP
# constructors are ever used, so file:/custom schemes cannot resolve here.
