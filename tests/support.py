"""Shared test doubles for the router's modules."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeUpstream:
    """An OpenAI-compatible upstream that answers every POST with one scripted reply."""

    def __init__(self, status=200, body=None, stream_pieces=None, delay=0.0, stall=0.0, script=(),
                 finish=None, pace=0.0):
        """delay holds back the whole reply; stall sends the headers, then holds back the body.
        script lists (status, body, delay) replies for the first requests, in order. finish is
        the stream's last frame before [DONE]: (finish_reason, usage). pace holds back each
        stream frame after the first, so a stream that keeps arriving can outlast a budget."""
        self.status, self.body, self.stream_pieces = status, body, stream_pieces
        self.finish, self.pace = finish, pace
        self.delay, self.stall, self.script = delay, stall, list(script)
        self.requests: list[tuple[dict, dict]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - matches the base signature
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                upstream.requests.append((dict(self.headers), json.loads(self.rfile.read(length))))
                status, body, delay = (upstream.script.pop(0) if upstream.script
                                       else (upstream.status, upstream.body, upstream.delay))
                frames = []
                if upstream.stream_pieces is not None and status == 200:
                    frames = [
                        f"data: {json.dumps({'model': 'fake', 'choices': [{'delta': delta}]})}\n\n"
                        for delta in upstream.stream_pieces
                    ]
                    if upstream.finish:
                        reason, usage = upstream.finish
                        frames.append("data: " + json.dumps({"model": "fake", "usage": usage,
                                                             "choices": [{"delta": {},
                                                                          "finish_reason": reason}]}) + "\n\n")
                    frames.append("data: [DONE]\n\n")
                    data = "".join(frames).encode()
                    content_type = "text/event-stream"
                else:
                    data = json.dumps(body).encode()
                    content_type = "application/json"
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(upstream.stall)
                    if upstream.pace and content_type == "text/event-stream":
                        for index, frame in enumerate(frames):
                            time.sleep(upstream.pace if index else 0)
                            self.wfile.write(frame.encode())
                            self.wfile.flush()
                    else:
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the router gave up on this upstream

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def completion(text):
    return {"model": "fake", "choices": [{"message": {"content": text},
                                          "finish_reason": "stop"}]}


def make_settings(**changes):
    """A Settings value with today's defaults, overridden per test."""
    from observer_router.settings import Settings

    fields = {"host": "127.0.0.1", "port": 1244, "allowed_hosts": (), "config_path": "",
              "break_after": 3, "break_seconds": 60.0, "max_prompt_chars": 240_000,
              "max_field_chars": 80_000, "min_retained_chars": 1_024,
              "base64_blob_chars": 16_384, "power_cache_seconds": 30.0,
              "idle_sample_seconds": 2.0, "group_strikes": 2, "request_deadline_seconds": 85.0,
              "repeat_window_seconds": 600.0}
    fields.update(changes)
    return Settings(**fields)


def make_runtime(config, chain=None, *, settings=None, probe=None, fetch=None, log=None):
    """A runtime for these provider tables, with the external edges pinned for a test."""
    from observer_router.config import parse_config
    from observer_router.log import log as default_log
    from observer_router.runtime import Runtime

    parsed = parse_config(config)
    return Runtime.build(
        settings or make_settings(), parsed, chain,
        probe=probe or (lambda: "AC Power"),
        fetch=fetch or (lambda url, signal: None),
        log=log or default_log)
