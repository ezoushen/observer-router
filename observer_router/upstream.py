"""The upstream call: connect, timeouts, the reasoning retry, streams and failures."""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from urllib.error import HTTPError

from observer_router.breakers import quota_reset
from observer_router.config import Provider
from observer_router.failure import TierFailure
from observer_router.idle import LaneLocks
from observer_router.log import log
from observer_router.prompt import message_chars
from observer_router.providers import headers_for, payload_for

MAX_ERROR_BODY_BYTES = 16_384


class UpstreamResponse:
    """File-like wrapper that closes both the response and its connection."""

    def __init__(self, connection, response, sock=None):
        self._connection = connection
        self._response = response
        self._sock = sock

    def read(self, size: int | None = None):
        if size is None:
            return self._response.read()
        return self._response.read(size)

    def __iter__(self):
        return iter(self._response)

    def set_timeout(self, seconds: float) -> None:
        """Bound the next reads; the socket keeps this until it is set again."""
        if self._sock is not None:
            self._sock.settimeout(seconds)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _connect(provider: Provider, timeout: float):
    """Open an http(s) connection for a provider, rejecting any other scheme."""
    parts = urllib.parse.urlsplit(provider.url)
    host = parts.hostname
    if not host:
        raise TierFailure(f"{provider.name}: URL has no host")
    if parts.scheme == "https":
        return http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout)
    if parts.scheme == "http":
        return http.client.HTTPConnection(host, parts.port or 80, timeout=timeout)
    raise TierFailure(f"{provider.name}: refusing non-http(s) URL")


def _timeout_kind(provider: Provider, error: BaseException, timeout: float) -> str:
    """ "timeout" for a read timeout that had the provider's whole budget; a timeout the request
    deadline cut short says nothing about the provider."""
    return "timeout" if isinstance(error, TimeoutError) and timeout >= provider.budget else ""


def _open(provider: Provider, body: dict, timeout: float, reasoning_mode: str | None):
    parts = urllib.parse.urlsplit(provider.url)
    payload = json.dumps(payload_for(provider, body, reasoning_mode)).encode()
    headers = headers_for(provider)
    headers["Content-Length"] = str(len(payload))
    connection = _connect(provider, timeout)
    try:
        connection.request("POST", parts.path or "/", body=payload, headers=headers)
        sock = connection.sock  # getresponse drops it from a connection that will close
        response = connection.getresponse()
    except Exception as error:
        connection.close()
        raise TierFailure(
            f"{provider.name}: {type(error).__name__}: {error}",
            overloaded=_timeout_kind(provider, error, timeout),
        ) from error
    if response.status >= 400:
        try:
            # Bounded but whole: a Gemini 429 names its quota only past the first ~1.5 KB.
            detail = response.read(MAX_ERROR_BODY_BYTES).decode("utf-8", "ignore")
        except Exception:
            detail = ""
        connection.close()
        error = HTTPError(
            provider.url, response.status, detail[:300], response.headers,
            BytesIO(detail.encode()),
        )
        setattr(error, "body_text", detail)
        raise error
    return UpstreamResponse(connection, response, sock)


def _error_body(error: HTTPError) -> str:
    """Return an upstream error body; the stream behind HTTPError can be read only once."""
    body = getattr(error, "body_text", None)
    if body is None:
        try:
            body = error.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        setattr(error, "body_text", body)
    return body


def _snippet(error: HTTPError) -> str:
    return _error_body(error)[:160]


def _http_failure(provider: Provider, error: HTTPError) -> TierFailure:
    quota = quota_reset(provider.type, error.code, error.headers, _error_body(error))
    name = provider.name
    if quota:
        retry_at, quota_name = quota
        return TierFailure(
            f"{name}: HTTP {error.code}: quota {quota_name} exhausted", retry_at, quota_name
        )
    # A 429 that names no quota (Gemini's bare RESOURCE_EXHAUSTED) has no reset to wait for, and
    # has been seen on every key of a group within the same minute: shared model capacity.
    overloaded = {503: "unavailable", 429: "exhausted"}.get(error.code, "")
    return TierFailure(f"{name}: HTTP {error.code}: {_snippet(error)}", overloaded=overloaded)


def _is_mandatory_reasoning_error(error: HTTPError) -> bool:
    if error.code != 400:
        return False
    detail = _error_body(error).lower()
    return "reasoning" in detail and "mandatory" in detail


def _open_with_reasoning_retry(
    provider: Provider, body: dict, timeout: float, reasoning_mode: str | None
):
    """Open a provider, retrying an OpenRouter endpoint once with reasoning.exclude when it
    refuses to run with reasoning disabled."""
    name = provider.name
    try:
        return _open(provider, body, timeout, reasoning_mode)
    except HTTPError as error:
        if provider.type == "openrouter" and _is_mandatory_reasoning_error(error):
            log(f"{name}: endpoint requires reasoning; retrying with reasoning.exclude")
            try:
                return _open(provider, body, timeout, "exclude")
            except HTTPError as retry_error:
                raise _http_failure(provider, retry_error) from retry_error
            except TierFailure:
                raise
            except Exception as retry_error:
                raise TierFailure(
                    f"{name}: {type(retry_error).__name__}: {retry_error}"
                ) from retry_error
        raise _http_failure(provider, error) from error
    except TierFailure:
        raise
    except Exception as error:
        raise TierFailure(f"{name}: {type(error).__name__}: {error}") from error


def _budget_left(budget: float, deadline: float) -> float:
    return max(1.0, min(budget, deadline - time.time()))


@contextmanager
def _lane(provider: Provider, lanes: LaneLocks, deadline: float):
    """Hold the provider's lane, waiting for it only while the request deadline still leaves the
    provider its minimum budget; past that, the provider is skipped rather than failed."""
    wait = deadline - time.time() - provider.min_budget
    if wait < 0:
        raise TierFailure(f"{provider.name}: skipped: under {provider.min_budget:g}s of the "
                          "request deadline left", skipped=True)
    lock = lanes.guard(provider)
    if not lock.acquire(timeout=wait):
        raise TierFailure(f"{provider.name}: skipped: lane still busy at the request deadline",
                          skipped=True)
    try:
        yield
    finally:
        lock.release()


def _rejected(provider: Provider, model: str | None) -> TierFailure | None:
    if model and model in provider.reject_models:
        return TierFailure(f"{provider.name}: served by rejected model {model}")
    return None


def _reasoning_mode(provider: Provider) -> str | None:
    return "disabled" if provider.type == "openrouter" else None


def iter_stream(provider: Provider, body: dict, deadline: float,
                lanes: LaneLocks) -> Iterator[tuple[str, str]]:
    """Yield ('meta', served_model) once, then ('delta', text) frames.

    Raises TierFailure when the provider produces no content or errors before the first
    content delta, so the caller can still fall through to the next provider. A buffer_stream
    provider yields nothing until its stream ends, so a reply cut off at the token limit
    (finish_reason=length) fails over too; any other provider's cut-off reply raises only after
    its text was forwarded.
    """
    with _lane(provider, lanes, deadline):
        yield from _iter_stream_locked(provider, body, deadline,
                                       _reasoning_mode(provider), lanes)


def _iter_stream_locked(provider, body, deadline, reasoning_mode,
                        lanes: LaneLocks) -> Iterator[tuple[str, str]]:
    timeout = _budget_left(provider.budget, deadline)
    # A buffered reply reaches the client only once it is whole, so the budget to first content
    # covers all of it: each read may wait only for what is left of it.
    whole_by = time.time() + timeout
    response = _open_with_reasoning_retry(provider, body, timeout, reasoning_mode)

    served = None
    finish = None
    usage = {}
    pieces: list[str] = []
    committed = False

    def bound_next_read():
        left = whole_by - time.time()
        if left <= 0:
            raise TierFailure(f"{provider.name}: reply not finished within {timeout:.0f}s",
                              overloaded=_timeout_kind(provider, TimeoutError(), timeout))
        response.set_timeout(left)

    try:
        if provider.buffer_stream:
            bound_next_read()
        for raw in _lines_until_content(provider, response, timeout, lambda: committed):
            if provider.buffer_stream:
                bound_next_read()
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            served = served or chunk.get("model")
            rejected = _rejected(provider, served)
            if rejected:
                raise rejected  # before any of its text is forwarded
            usage = chunk.get("usage") or usage
            choice = (chunk.get("choices") or [{}])[0]
            finish = choice.get("finish_reason") or finish
            piece = (choice.get("delta") or {}).get("content")
            if not piece:
                continue  # reasoning deltas are intentionally dropped
            pieces.append(piece)
            if provider.buffer_stream:
                continue  # held back until finish_reason says whether the reply is whole
            if not committed:
                committed = True
                yield ("meta", served or provider.model)
            yield ("delta", piece)
    finally:
        response.close()
    if not pieces:
        raise TierFailure(f"{provider.name}: stream produced no content")
    if finish == "length":
        raise _cut_off(provider, body, "".join(pieces), usage, served or provider.model)
    if not committed and not finish:
        raise TierFailure(f"{provider.name}: stream ended without a finish_reason")
    if not committed:
        yield ("meta", served or provider.model)
        yield ("delta", "".join(pieces))


def _lines_until_content(provider: Provider, response, timeout: float, committed) -> Iterator[bytes]:
    """Yield the response's lines. A read error before the stream commits -- an upstream that
    sent its headers and then stalled -- becomes a TierFailure, so the chain can still fail over;
    after it, the error propagates to the committed stream's handler."""
    lines = iter(response)
    while True:
        try:
            raw = next(lines)
        except StopIteration:
            return
        except (OSError, http.client.HTTPException) as error:
            if committed():
                raise
            raise TierFailure(
                f"{provider.name}: {type(error).__name__} before first content: {error}",
                overloaded=_timeout_kind(provider, error, timeout),
            ) from error
        yield raw


def nonstream(provider: Provider, body: dict, deadline: float, lanes: LaneLocks):
    """Return (response_dict, content) for a provider, or raise TierFailure."""
    with _lane(provider, lanes, deadline):
        timeout = _budget_left(provider.budget, deadline)
        response = _open_with_reasoning_retry(provider, body, timeout, _reasoning_mode(provider))
        try:
            payload = json.load(response)
        except Exception as error:
            raise TierFailure(
                f"{provider.name}: unreadable response: {error}",
                overloaded=_timeout_kind(provider, error, timeout),
            ) from error
        finally:
            response.close()
    rejected = _rejected(provider, payload.get("model"))
    if rejected:
        raise rejected
    choice = (payload.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        raise TierFailure(
            f"{provider.name}: empty content (finish_reason={choice.get('finish_reason')})"
        )
    if choice.get("finish_reason") == "length":
        raise _cut_off(provider, body, content, payload.get("usage") or {},
                       payload.get("model") or provider.model)
    return payload, content


CUT_OFF_PREVIEW_CHARS = 80


def _cut_off(provider: Provider, body: dict, text: str, usage: dict, model: str) -> TierFailure:
    """Log a reply that hit the token limit, then return its failure.

    The head and tail are logged because no body is kept anywhere else: they are what tells a
    model stuck in a loop from a genuinely long answer.
    """
    usage = usage if isinstance(usage, dict) else {}
    log(
        f"tier {provider.name} cut off at the token limit: "
        f"prompt_tokens={usage.get('prompt_tokens', '?')} "
        f"prompt_chars={message_chars(body.get('messages') or [])} "
        f"completion_tokens={usage.get('completion_tokens', '?')} chars={len(text)} "
        f"head={text[:CUT_OFF_PREVIEW_CHARS]!r} tail={text[-CUT_OFF_PREVIEW_CHARS:]!r}"
    )
    return TierFailure(f"{provider.name}: reply cut off at the token limit", partial=(model, text))
