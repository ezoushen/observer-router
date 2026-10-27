"""The OpenAI-compatible HTTP surface.

`/health`, `/v1/models` and `/v1/chat/completions`; the chat route fails over across the chain
and commits to a stream only once its first content delta arrives.
"""

from __future__ import annotations

import ipaddress
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from observer_router.failure import TierFailure
from observer_router.log import log
from observer_router.runtime import Runtime
from observer_router.upstream import iter_stream, nonstream


def _now() -> int:
    try:
        return int(time.time())
    except Exception:
        return 0


class RouterServer(ThreadingHTTPServer):
    """A threading server that carries the runtime its handler serves.

    Holding the runtime on the server keeps one bound handler class, so a test can swap the
    chain under a live server without rebinding.
    """

    runtime: Runtime
    daemon_threads = True


def serve(runtime: Runtime, host: str, port: int) -> RouterServer:
    """Bind a server for this runtime; it is not started until `serve_forever` runs."""
    server = RouterServer((host, port), RouterHandler)
    server.runtime = runtime
    return server


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def runtime(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002 - matches base signature
        return  # access lines would drown the tier log

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # an IPv6 literal, with or without a port
            return True
        name = (host.rpartition(":")[0] if host.count(":") == 1 else host).removesuffix(".")
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return not name or name == "localhost" or name in self.runtime.settings.allowed_hosts

    def _refuse(self, status: int, message: str, header: str = "") -> None:
        """Answer an error before the request body is read, then end the connection: an unread
        body would otherwise be parsed as the next request on a kept-alive connection, which lets
        a page that may only post text smuggle in a POST that looks like JSON."""
        self.close_connection = True
        shown = f": {header}={(self.headers.get(header) or '')[:80]!r}" if header else ""
        log(f"refused {status} {self.command} {self.path[:80]}{shown}")
        self._json(status, {"error": message}, close=True)

    def _refuse_foreign_host(self) -> bool:
        if self._host_allowed():
            return False
        self._refuse(403, "host not allowed", "Host")
        return True

    def _json(self, status: int, payload: dict, close: bool = False) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if close:
                self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _chunk(self, text: str) -> None:
        data = text.encode()
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")

    def _end_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")

    @staticmethod
    def _sse_delta(model: str, text: str, finish_reason: str | None = None) -> str:
        chunk = {
            "id": "chatcmpl-router",
            "object": "chat.completion.chunk",
            "created": _now(),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text} if text else {},
                         "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    def do_GET(self):
        if self._refuse_foreign_host():
            return
        route = self.path.rstrip("/")
        if route == "/health":
            self._json(200, self.runtime.health())
        elif route == "/v1/models":
            self._json(200, {
                "object": "list",
                "data": [{"id": "observer-router", "object": "model", "owned_by": "local"}],
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        self._arrived = time.time()  # the request deadline, and a served line's age, count from here
        if self._refuse_foreign_host():
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._refuse(404, "not found")
            return
        # A cross-site form or text post cannot declare JSON without a CORS preflight, which the
        # router never answers.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() \
                != "application/json":
            self._refuse(415, "Content-Type must be application/json", "Content-Type")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError
        except ValueError:
            self._refuse(400, "invalid Content-Length")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON body"})
            return
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            self._json(400, {"error": "messages must be an array"})
            return

        body = dict(body)
        body["messages"], compacted = self.runtime.compact(body["messages"])
        self.runtime.record_compaction(compacted)
        if compacted["changed"]:
            log(
                "compacted prompt "
                f"chars={compacted['input_chars']}->{compacted['output_chars']} "
                f"dropped_messages={compacted['dropped_messages']} "
                f"folded_fields={compacted['folded_fields']} "
                f"removed_images={compacted['removed_image_parts']} "
                f"removed_blobs={compacted['removed_blobs']}"
            )

        order = self.runtime.expand_chain()  # once per request: every expansion advances the group rotation
        # One deadline for the whole chain: a reply later than the client waits reaches no one.
        deadline = self._arrived + self.runtime.settings.request_deadline_seconds
        if body.get("stream"):
            self._stream(body, order, deadline)
        else:
            self._once(body, order, deadline)

    # --- streaming path -------------------------------------------------------

    def _stream(self, body: dict, order: list[str], deadline: float) -> None:
        attempted = []
        salvage = None  # (tier, (model, text)) of the first reply cut off at the token limit
        allowed, skipped = self.runtime.allowed_tiers(order)
        for tier, reason in skipped.items():
            attempted.append(f"{tier}({reason})")
        for tier in allowed:
            # The idle gate is re-read here: earlier providers can take tens of seconds, and a
            # lane that became busy meanwhile must still keep a local provider out.
            provider = self.runtime.providers[tier]
            skip_reason = self.runtime.skip_reason(tier) or self.runtime.busy_reason(provider)
            if skip_reason:
                attempted.append(f"{tier}({skip_reason})")
                continue
            started = time.time()
            try:
                generator = iter_stream(provider, self.runtime.fit(provider, body), deadline, self.runtime.lanes)
                _, served = next(generator)  # blocks until the first content delta
            except StopIteration:
                self.runtime.failed(tier, "no frames")
                attempted.append(f"{tier}(no frames)")
                log(f"tier {tier} failed: stream ended with no frames")
                continue
            except TierFailure as failure:
                self.runtime.record_failure(tier, failure)
                attempted.append(f"{tier}({failure})")
                log(f"tier {tier} failed: {failure}")
                salvage = salvage or (failure.partial and (tier, failure.partial))
                continue

            # First content delta is in hand: commit to this tier and stop buffering.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _, piece in generator:
                    self._chunk(self._sse_delta(served, piece))
                self._chunk("data: [DONE]\n\n")
                self._end_chunks()
                self.runtime.ok(tier)
                log(
                    f"served tier={tier} model={served} "
                    f"latency={time.time() - started:.2f}s "
                    f"age={time.time() - self._arrived:.2f}s stream=1"
                )
            except Exception as error:  # mid-stream failure: the connection is committed
                log(f"tier {tier} failed mid-stream: {type(error).__name__}: {error}")
                cut_off = bool(getattr(error, "partial", None))
                if not cut_off:
                    self.runtime.failed(tier, f"mid-stream {type(error).__name__}")
                try:
                    if cut_off:  # tell the client, which keeps a cut-off reply's complete blocks
                        self._chunk(self._sse_delta(served, "", "length"))
                    self._chunk("data: [DONE]\n\n")
                    self._end_chunks()
                except Exception as error:
                    log(f"failed to close stream for tier {tier}: {type(error).__name__}")
            return

        if salvage:
            tier, (served, text) = salvage
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                self._chunk(self._sse_delta(served, text))
                self._chunk(self._sse_delta(served, "", "length"))
                self._chunk("data: [DONE]\n\n")
                self._end_chunks()
                log(f"served tier={tier} model={served} cut off, no later tier answered stream=1")
            except Exception as error:
                log(f"failed to send the cut-off reply of tier {tier}: {type(error).__name__}")
            return
        self._json(502, {"error": "all tiers failed", "attempted": attempted})

    # --- non-streaming path ---------------------------------------------------

    def _once(self, body: dict, order: list[str], deadline: float) -> None:
        attempted = []
        salvage = None  # (tier, (model, text)) of the first reply cut off at the token limit
        allowed, skipped = self.runtime.allowed_tiers(order)
        for tier, reason in skipped.items():
            attempted.append(f"{tier}({reason})")
        for tier in allowed:
            # The idle gate is re-read here: earlier providers can take tens of seconds, and a
            # lane that became busy meanwhile must still keep a local provider out.
            provider = self.runtime.providers[tier]
            skip_reason = self.runtime.skip_reason(tier) or self.runtime.busy_reason(provider)
            if skip_reason:
                attempted.append(f"{tier}({skip_reason})")
                continue
            started = time.time()
            try:
                payload, content = nonstream(provider, self.runtime.fit(provider, body), deadline, self.runtime.lanes)
            except TierFailure as failure:
                self.runtime.record_failure(tier, failure)
                attempted.append(f"{tier}({failure})")
                log(f"tier {tier} failed: {failure}")
                salvage = salvage or (failure.partial and (tier, failure.partial))
                continue
            served = payload.get("model") or tier
            choice = (payload.get("choices") or [{}])[0]
            self.runtime.ok(tier)
            log(
                f"served tier={tier} model={served} "
                f"latency={time.time() - started:.2f}s "
                f"age={time.time() - self._arrived:.2f}s stream=0"
            )
            self._json(200, {
                "id": payload.get("id") or "chatcmpl-router",
                "object": "chat.completion",
                "created": payload.get("created") or _now(),
                "model": served,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": choice.get("finish_reason") or "stop",
                }],
                "usage": payload.get("usage") or {},
            })
            return
        if salvage:
            tier, (served, text) = salvage
            log(f"served tier={tier} model={served} cut off, no later tier answered stream=0")
            self._json(200, {
                "id": "chatcmpl-router",
                "object": "chat.completion",
                "created": _now(),
                "model": served,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "length",
                }],
                "usage": {},
            })
            return
        self._json(502, {"error": "all tiers failed", "attempted": attempted})
