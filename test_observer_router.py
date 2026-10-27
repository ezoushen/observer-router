#!/usr/bin/env python3
"""Contract tests for observer-router: prompt compaction, the power and idle gates, the cursor
tier, and quota-reset blocks."""

import contextlib
import http.client
import importlib.util
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from observer_router.settings import build_settings

from tests.support import FakeUpstream, completion


MODULE_PATH = Path(__file__).with_name("observer-router.py")
SPEC = importlib.util.spec_from_file_location("observer_router_legacy", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {MODULE_PATH}")
router: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(router)


def _config(text: str) -> Any:
    """Parse a TOML config through the public loader, via a temporary file."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(text)
        return router.load_config(str(path))


POOL = """
chain = ["pool", "local"]

[providers.a]
type = "openai"
url = "http://127.0.0.1:9/a"
model = "m"

[providers.b]
type = "openai"
url = "http://127.0.0.1:9/b"
model = "m"

[providers.c]
type = "openai"
url = "http://127.0.0.1:9/c"
model = "m"

[providers.local]
type = "openai"
url = "http://127.0.0.1:9/local"
model = "m"

[groups.pool]
members = ["a", "b", "c"]
"""


class GroupRotationTests(unittest.TestCase):
    """A group starts each request one member further on, so its keys share the load."""

    def test_a_group_rotates_its_members_across_requests(self):
        config = _config(POOL)
        router.configure(config)
        try:
            orders = [router.expand_chain() for _ in range(4)]
        finally:
            router.configure(router.Config({}, ()))

        self.assertEqual(config.chain, ("pool", "local"))
        self.assertEqual(config.groups["pool"], ("a", "b", "c"))
        self.assertEqual(orders, [
            ["a", "b", "c", "local"],
            ["b", "c", "a", "local"],
            ["c", "a", "b", "local"],
            ["a", "b", "c", "local"],
        ])


def _use(tables: dict, chain=None) -> None:
    """Make these provider tables the router's active configuration."""
    config = router.parse_config({"providers": tables})
    router.configure(config, tuple(chain) if chain else None)


REMOTE = {"type": "openai", "url": "http://127.0.0.1:9/v1/chat/completions", "model": "r"}
LANE = {**REMOTE, "model": "lane", "serial": True, "ac_only": True}


class ActiveConfigTestCase(unittest.TestCase):
    """Restores the active configuration after each test."""

    def setUp(self):
        self.saved_config = (router.PROVIDERS, router.CHAIN, router.GROUPS)

    def tearDown(self):
        router.PROVIDERS, router.CHAIN, router.GROUPS = self.saved_config


class PowerGateTests(ActiveConfigTestCase):
    """A GPU lane marked ac_only is left out while on battery; other providers still run.

    The gate is pinned through its probe, so the tests never shell out to pmset.
    """

    def setUp(self):
        super().setUp()
        _use({"remote": REMOTE, "local": LANE})
        self.saved_power = router.POWER

    def tearDown(self):
        super().tearDown()
        router.POWER = self.saved_power

    def _pin_power(self, source: str) -> None:
        router.POWER = router.PowerGate(lambda: source, cache_seconds=30.0)

    def test_ac_only_provider_runs_on_ac_power(self):
        self._pin_power("AC Power")

        allowed, skipped = router.allowed_tiers()

        self.assertEqual(allowed, ["remote", "local"])
        self.assertEqual(skipped, {})

    def test_ac_only_provider_is_skipped_on_battery_with_a_reason(self):
        self._pin_power("Battery Power")

        allowed, skipped = router.allowed_tiers()

        self.assertEqual(allowed, ["remote"])
        self.assertIn("battery", skipped["local"])

    def test_unreadable_power_state_keeps_the_provider(self):
        self._pin_power("unknown")

        allowed, _ = router.allowed_tiers()

        self.assertIn("local", allowed)

    def test_a_provider_without_ac_only_runs_on_battery(self):
        _use({"remote": REMOTE, "local": {**LANE, "ac_only": False}})
        self._pin_power("Battery Power")

        allowed, skipped = router.allowed_tiers()

        self.assertEqual(allowed, ["remote", "local"])
        self.assertEqual(skipped, {})

    def test_snapshot_reports_the_gate(self):
        self._pin_power("Battery Power")

        snapshot = router.power_snapshot()

        self.assertEqual(snapshot["source"], "Battery Power")
        self.assertEqual(snapshot["ac_only"], ["local"])
        self.assertFalse(snapshot["ac_only_allowed"])


def _gemini_429(quota_ids, retry_delay="34s"):
    """A Gemini 429 in the shape the OpenAI-compatible endpoint returned on 2026-09-24."""
    return json.dumps([{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
             "quotaId": quota_id} for quota_id in quota_ids
        ]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
    ]}}], indent=2)


class BreakerInterplayTests(ActiveConfigTestCase):
    """Recording a failure blocks the tier at its quota reset or at the breaker threshold."""

    # 2026-09-24 07:28:25 UTC = 00:28:25 PDT
    NOW = 1790234905.0

    def setUp(self):
        super().setUp()
        _use({"gemini": {**REMOTE, "type": "gemini", "api_key_env": "K"}})
        self.saved = router.BREAKERS

    def tearDown(self):
        super().tearDown()
        router.BREAKERS = self.saved

    def test_quota_failure_blocks_the_tier_with_its_reason(self):
        until = router.time.time() + 3600
        router.record_failure("gemini", router.TierFailure("x", until, "PerDayQuota"))

        reason = router.tier_skip_reason("gemini")

        self.assertIn("quota PerDayQuota until", reason)
        self.assertFalse(router.tier_available("gemini"))
        self.assertIn("quota PerDayQuota", router.breaker_snapshot()["gemini"]["reason"])

    def test_breaker_cannot_shorten_a_quota_block(self):
        until = router.time.time() + 3600
        router.record_failure("gemini", router.TierFailure("x", until, "PerDayQuota"))
        for _ in range(router.BREAK_AFTER):
            router.record_failure("gemini", router.TierFailure("gemini: HTTP 503"))

        self.assertIn("quota", router.tier_skip_reason("gemini"))

    def test_success_clears_the_block(self):
        router.record_failure("gemini", router.TierFailure("x", router.time.time() + 60, "q"))

        router.tier_ok("gemini")

        self.assertIsNone(router.tier_skip_reason("gemini"))


SPLASH_METRICS = """# HELP splash_requests_submitted_total Requests submitted.
splash_frontend_active 0
splash_requests_submitted_total {submitted}
splash_requests_completed_total {completed}
splash_requests_cancelled_total 10
splash_requests_failed_total 0
splash_scheduler_decoding 1
"""


class IdleGateTests(ActiveConfigTestCase):
    """A provider with idle_metrics waits until every watched lane has been idle long enough."""

    URL = "http://127.0.0.1:1240/metrics"

    def setUp(self):
        super().setUp()
        _use({"remote": REMOTE,
              "local": {**LANE, "ac_only": False, "idle_metrics": [self.URL], "idle_seconds": 30}})
        self.local = router.PROVIDERS["local"]
        self.saved_idle = router.IDLE
        self.idle = router.IdleMonitor(fetch=lambda url, signal: None, sample_seconds=2.0)
        router.IDLE = self.idle

    def tearDown(self):
        super().tearDown()
        router.IDLE = self.saved_idle

    def test_busy_lane_keeps_the_provider_out_with_a_reason(self):
        self.idle.record(self.URL, (1011, 1), router.time.time())

        allowed, skipped = router.allowed_tiers()

        self.assertEqual(allowed, ["remote"])
        self.assertIn("lanes busy (127.0.0.1:1240", skipped["local"])
        self.assertFalse(router.idle_snapshot()["local"]["allowed"])

    def test_the_sampler_watches_only_lanes_of_providers_in_the_chain(self):
        self.assertEqual(router.watched_lanes(), (self.URL,))

        _use({"remote": REMOTE, "local": {**LANE, "idle_metrics": [self.URL]}}, chain=["remote"])

        self.assertEqual(router.watched_lanes(), ())

    def test_gate_is_off_without_watched_lanes(self):
        _use({"local": LANE})

        self.assertIsNone(router.busy_reason(router.PROVIDERS["local"]))


class RequestGuardTests(ActiveConfigTestCase):
    """A web page must not be able to drive the router: it takes neither a Host header that a
    rebound DNS name would carry, nor a POST that is not declared JSON."""

    def setUp(self):
        super().setUp()
        self.upstream = FakeUpstream(body=completion("hi"))
        _use({"p": {"type": "openai", "url": self.upstream.url, "model": "m"}})
        self.saved_hosts = router.ALLOWED_HOSTS
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), router.RouterHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port

    def tearDown(self):
        super().tearDown()
        router.ALLOWED_HOSTS = self.saved_hosts
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()

    def _call(self, method, path, headers, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            self.last_connection_header = response.getheader("Connection")
            return response.status, response.read()
        finally:
            connection.close()

    CHAT = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()

    def test_a_host_header_naming_another_site_is_refused(self):
        # Rule: a DNS name that is not localhost or listed reaches the router only through a
        # rebound page; GET and POST are refused with 403 and no provider is called.
        for method, path, body in (("GET", "/health", None),
                                   ("POST", "/v1/chat/completions", self.CHAT)):
            with self.subTest(method):
                status, _ = self._call(method, path, {"Host": "evil.example:1244",
                                                      "Content-Type": "application/json"}, body)
                self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])

    def test_a_refusal_closes_the_connection(self):
        # Rule: a refused POST leaves its body unread, so the connection must not be reused,
        # or the body would be parsed as the next request.
        for label, headers in (("host", {"Host": "evil.example", "Content-Type": "application/json"}),
                               ("type", {"Content-Type": "text/plain"})):
            with self.subTest(label):
                self._call("POST", "/v1/chat/completions", headers, self.CHAT)
                self.assertEqual(self.last_connection_header, "close")

    def _raw(self, payload: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            connection.sendall(payload)
            connection.settimeout(1.0)
            received = b""
            try:
                while chunk := connection.recv(65536):
                    received += chunk
            except TimeoutError:
                pass
            return received

    def test_a_body_left_unread_is_never_parsed_as_the_next_request(self):
        # Rule: whatever refuses a POST, its body cannot become a second request on the same
        # connection. A page can post text to http://127.0.0.1:1244/anything (an IP literal passes
        # the Host check); if that body were parsed, it would carry the JSON the guard demands.
        inner = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                 b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                 % (len(self.CHAT), self.CHAT))
        for label, head in (("unknown path", b"POST /anything HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                              b"Content-Type: text/plain\r\n"),
                            ("wrong type", b"POST /v1/chat/completions HTTP/1.1\r\n"
                                           b"Host: 127.0.0.1\r\nContent-Type: text/plain\r\n")):
            with self.subTest(label):
                request = head + b"Content-Length: %d\r\n\r\n" % len(inner) + inner
                answered = self._raw(request).count(b"HTTP/1.")
                self.assertEqual(answered, 1)
        self.assertEqual(self.upstream.requests, [])

    def test_an_unusable_content_length_is_refused_and_closes_the_connection(self):
        # Rule: a Content-Length that is not a non-negative integer cannot say where the body
        # ends, so the request is refused rather than read as empty and the body left behind.
        for value in ("abc", "-5"):
            with self.subTest(value):
                reply = self._raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                  b"Content-Type: application/json\r\nContent-Length: " +
                                  value.encode() + b"\r\n\r\n{}")
                self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply[:80])
                self.assertIn(b"Connection: close", reply)

    def test_a_refusal_is_logged_with_what_was_refused(self):
        # Rule: a legitimate client that is turned away must be findable in the log: the status,
        # the request line, and the offending header, quoted and cut short.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._call("GET", "/health", {"Host": "evil.example"})
            self._call("POST", "/v1/chat/completions", {"Content-Type": "text/plain" + "x" * 300},
                       self.CHAT)
        lines = [line for line in out.getvalue().splitlines() if "refused" in line]
        self.assertEqual(len(lines), 2, out.getvalue())
        self.assertIn("refused 403 GET /health: Host='evil.example'", lines[0])
        self.assertIn("refused 415 POST /v1/chat/completions: Content-Type='text/plain", lines[1])
        self.assertNotIn("x" * 150, lines[1])

    def test_a_trailing_dot_does_not_change_the_host(self):
        # Rule: "localhost." is the same name as "localhost", so a browser that sends it is served;
        # a listed name is matched the same way, and another name stays refused with or without it.
        router.ALLOWED_HOSTS = ("router.lan",)
        for host, expected in (("localhost.:1244", 200), ("router.lan.", 200),
                               ("evil.example.", 403)):
            with self.subTest(host):
                status, _ = self._call("GET", "/health", {"Host": host})
                self.assertEqual(status, expected)

    def test_only_application_json_itself_is_taken(self):
        # Rule: a media type that merely starts like it, or lists another, is not JSON.
        for content_type in ("application/jsonx", "application/json,text/plain",
                             "text/plain, application/json", "application/x-ndjson",
                             "application/vnd.api+json"):
            with self.subTest(content_type):
                status, _ = self._call("POST", "/v1/chat/completions",
                                       {"Content-Type": content_type}, self.CHAT)
                self.assertEqual(status, 415)

    def test_loopback_and_literal_address_hosts_are_served(self):
        # Rule: an IP literal or localhost cannot be a rebound name, so any of them is served;
        # this keeps LAN access (a bound address) working.
        for host in ("localhost:1244", "127.0.0.1:1244", "[::1]:1244", "192.168.1.5:1244",
                     "127.0.0.1"):
            with self.subTest(host):
                status, _ = self._call("GET", "/health", {"Host": host})
                self.assertEqual(status, 200)

    def test_listed_host_names_are_read_trimmed_and_lower_cased(self):
        # Rule: OBSERVER_ROUTER_ALLOWED_HOSTS is a comma list; case and spaces do not matter.
        settings, errors = build_settings(
            {"OBSERVER_ROUTER_ALLOWED_HOSTS": " Router.LAN , nas.local,"})
        self.assertEqual(errors, ())
        self.assertEqual(settings.allowed_hosts, ("router.lan", "nas.local"))

    def test_a_listed_host_name_is_served(self):
        # Rule: OBSERVER_ROUTER_ALLOWED_HOSTS names the DNS names that may reach the router.
        router.ALLOWED_HOSTS = ("router.lan",)
        status, _ = self._call("GET", "/health", {"Host": "Router.LAN:1244"})
        self.assertEqual(status, 200)

    def test_a_post_that_is_not_declared_json_is_refused(self):
        # Rule: a cross-site form or text post carries another type, or none; only
        # application/json (with any parameters) is taken, so no provider is called otherwise.
        for label, headers in (("text", {"Content-Type": "text/plain"}),
                               ("form", {"Content-Type": "application/x-www-form-urlencoded"}),
                               ("none", {})):
            with self.subTest(label):
                status, _ = self._call("POST", "/v1/chat/completions", headers, self.CHAT)
                self.assertEqual(status, 415)
        self.assertEqual(self.upstream.requests, [])
        status, _ = self._call("POST", "/v1/chat/completions",
                               {"Content-Type": "Application/JSON; charset=utf-8"}, self.CHAT)
        self.assertEqual(status, 200)


class ChainTests(ActiveConfigTestCase):
    """Requests through the router's HTTP surface fail over between named instances."""

    def setUp(self):
        super().setUp()
        self.env = dict(router.os.environ)
        router.os.environ.update({"TEST_GEMINI_A": "key-a", "TEST_GEMINI_B": "key-b"})
        self.saved_breaker = router.BREAKERS
        self.upstreams: list[FakeUpstream] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), router.RouterHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        super().tearDown()
        self.server.shutdown()
        self.server.server_close()
        for upstream in self.upstreams:
            upstream.close()
        router.os.environ.clear()
        router.os.environ.update(self.env)
        router.BREAKERS = self.saved_breaker

    def _upstream(self, **kwargs) -> FakeUpstream:
        upstream = FakeUpstream(**kwargs)
        self.upstreams.append(upstream)
        return upstream

    def _two_geminis(self, first: FakeUpstream, second: FakeUpstream) -> None:
        _use({
            "gemini-a": {"type": "gemini", "url": first.url, "model": "model-a",
                         "api_key_env": "TEST_GEMINI_A"},
            "gemini-b": {"type": "gemini", "url": second.url, "model": "model-b",
                         "api_key_env": "TEST_GEMINI_B"},
        })

    def _post(self, body: dict) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{self.base}/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def _get_health(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/health", timeout=10) as response:
            return json.load(response)

    def test_a_quota_blocked_instance_fails_over_to_a_second_instance_of_the_same_type(self):
        first = self._upstream(status=429, body=json.loads(_gemini_429(
            ["GenerateRequestsPerDayPerProjectPerModel-FreeTier"]))[0])
        second = self._upstream(body=completion("from b"))
        self._two_geminis(first, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "from b")
        self.assertEqual(first.requests[0][0]["Authorization"], "Bearer key-a")
        self.assertEqual(first.requests[0][1]["model"], "model-a")
        self.assertEqual(second.requests[0][0]["Authorization"], "Bearer key-b")
        self.assertEqual(second.requests[0][1]["model"], "model-b")
        breaker = self._get_health()["breaker"]
        self.assertIn("quota GenerateRequestsPerDay", breaker["gemini-a"]["reason"])
        self.assertEqual(breaker["gemini-b"]["reason"], "")

    def test_a_blocked_instance_is_skipped_on_the_next_request(self):
        first = self._upstream(status=429, body=json.loads(_gemini_429(
            ["GenerateRequestsPerDayPerProjectPerModel-FreeTier"]))[0])
        second = self._upstream(body=completion("from b"))
        self._two_geminis(first, second)

        self._post({"messages": [{"role": "user", "content": "one"}]})
        self._post({"messages": [{"role": "user", "content": "two"}]})

        self.assertEqual(len(first.requests), 1)
        self.assertEqual(len(second.requests), 2)

    def test_an_empty_stream_fails_over_and_the_next_instance_streams_content(self):
        first = self._upstream(stream_pieces=[{"reasoning": "thinking"}, {"content": ""}])
        second = self._upstream(stream_pieces=[{"content": "hel"}, {"content": "lo"}])
        self._two_geminis(first, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        frames = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith("data: {")]
        self.assertEqual("".join(f["choices"][0]["delta"]["content"] for f in frames), "hello")

    def test_all_instances_failing_names_each_one(self):
        first = self._upstream(status=503, body={"error": "down"})
        second = self._upstream(body=completion(""))
        self._two_geminis(first, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 502)
        attempted = json.loads(raw)["attempted"]
        self.assertTrue(attempted[0].startswith("gemini-a("), attempted)
        self.assertTrue(attempted[1].startswith("gemini-b("), attempted)
        self.assertIn("empty content", attempted[1])

    def test_a_reply_cut_off_at_the_token_limit_fails_over(self):
        # Rule: finish_reason=length is a truncated reply, not a result; the next tier answers.
        cut = self._upstream(body={"model": "fake", "choices": [
            {"message": {"content": "1 2 3 4 5 6"}, "finish_reason": "length"}]})
        second = self._upstream(body=completion("from b"))
        self._two_geminis(cut, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "from b")
        self.assertEqual(len(second.requests), 1)

    def _buffered_then_fallback(self, first: FakeUpstream, fallback: FakeUpstream) -> None:
        _use({
            "lane": {"type": "openai", "url": first.url, "model": "lane", "buffer_stream": True},
            "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"},
        })

    @staticmethod
    def _streamed_text(raw: bytes) -> str:
        frames = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith("data: {")]
        return "".join(f["choices"][0]["delta"].get("content", "") for f in frames)

    def test_a_buffered_stream_cut_off_at_the_token_limit_fails_over(self):
        # Rule: a buffered provider's stream that ends finish_reason=length is never forwarded;
        # the next tier answers in full.
        cut = self._upstream(stream_pieces=[{"content": "loop "}, {"content": "loop "}],
                             finish=("length", {"prompt_tokens": 4270, "completion_tokens": 2048}))
        fallback = self._upstream(stream_pieces=[{"content": "fall"}, {"content": "back"}])
        self._buffered_then_fallback(cut, fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")
        # A cut-off depends on the prompt, not the provider: it does not count toward the breaker.
        self.assertEqual(self._get_health()["breaker"]["lane"]["failures"], 0)

    def test_a_buffered_stream_that_finishes_is_served_whole(self):
        # Rule: buffering changes when the text is sent, never what is sent.
        lane = self._upstream(stream_pieces=[{"content": "hel"}, {"content": "lo"}],
                              finish=("stop", {"prompt_tokens": 9, "completion_tokens": 2}))
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._buffered_then_fallback(lane, fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "hello")
        self.assertEqual(fallback.requests, [])

    def test_a_cut_off_reply_is_logged_with_its_size_and_both_ends(self):
        # Rule: one log line names the provider, prompt size, completion tokens and a short
        # head/tail of the text, so looping can be told from a genuinely long answer.
        text = "BEGIN" + "x" * 300 + "END"
        cut = self._upstream(stream_pieces=[{"content": text}],
                             finish=("length", {"prompt_tokens": 4270, "completion_tokens": 2048}))
        self._buffered_then_fallback(cut, self._upstream(stream_pieces=[{"content": "ok"}]))

        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        lines = [line for line in out.getvalue().splitlines() if "completion_tokens=" in line]
        self.assertEqual(len(lines), 1, out.getvalue())
        line = lines[0]
        self.assertIn("tier lane cut off at the token limit", line)
        self.assertIn("prompt_tokens=4270", line)
        self.assertIn("prompt_chars=2", line)
        self.assertIn("completion_tokens=2048", line)
        self.assertIn("head='BEGINxxx", line)
        self.assertIn("xxxEND'", line)
        self.assertNotIn("x" * 200, line)  # a preview, not the whole reply

    def test_an_unbuffered_stream_cut_off_is_still_logged_but_not_served_as_a_success(self):
        # Rule: without buffer_stream the text is already forwarded, so the cut cannot fail over;
        # it is still logged with its size and never recorded as a served reply. A cut-off says
        # nothing about the provider's health, so it does not count toward the breaker.
        cut = self._upstream(stream_pieces=[{"content": "partial"}],
                             finish=("length", {"prompt_tokens": 7, "completion_tokens": 3}))
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": cut.url, "model": "lane"},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        with contextlib.redirect_stdout(io.StringIO()) as out:
            status, raw = self._post({"messages": [{"role": "user", "content": "hi"}],
                                      "stream": True})

        self.assertEqual(self._streamed_text(raw), "partial")
        last = [json.loads(line[6:]) for line in raw.decode().splitlines()
                if line.startswith("data: {")][-1]
        self.assertEqual(last["choices"][0]["finish_reason"], "length")  # so the client knows
        self.assertIn("tier lane cut off at the token limit: prompt_tokens=7", out.getvalue())
        self.assertNotIn("served tier=lane", out.getvalue())
        self.assertEqual(self._get_health()["breaker"]["lane"]["failures"], 0)
        self.assertEqual(fallback.requests, [])

    def test_a_buffered_stream_that_ends_without_a_finish_reason_fails_over(self):
        # Rule: a buffered stream is served only once finish_reason shows it is whole; a stream
        # that ends without one (a lane that stopped mid-reply) is a failed attempt.
        dropped = self._upstream(stream_pieces=[{"content": "half a sent"}])
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._buffered_then_fallback(dropped, fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")

    def test_a_cut_off_reply_with_malformed_usage_still_fails_over(self):
        # Rule: whatever the upstream puts in usage, a cut-off reply is a failed attempt.
        cut = self._upstream(body={"model": "fake", "usage": "n/a", "choices": [
            {"message": {"content": "partial"}, "finish_reason": "length"}]})
        second = self._upstream(body=completion("from b"))
        self._two_geminis(cut, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "from b")

    def test_a_cut_off_non_streaming_reply_is_logged_with_its_size_and_both_ends(self):
        # Rule: the cut-off log line is written on the non-streaming path too.
        text = "BEGIN" + "y" * 300 + "END"
        cut = self._upstream(body={"model": "fake", "usage": {"prompt_tokens": 5109,
                                                              "completion_tokens": 2048},
                                   "choices": [{"message": {"content": text},
                                                "finish_reason": "length"}]})
        self._two_geminis(cut, self._upstream(body=completion("from b")))

        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._post({"messages": [{"role": "user", "content": "hello"}]})

        lines = [line for line in out.getvalue().splitlines() if "completion_tokens=" in line]
        self.assertEqual(len(lines), 1, out.getvalue())
        self.assertIn("tier gemini-a cut off at the token limit: prompt_tokens=5109 "
                      "prompt_chars=5 completion_tokens=2048 chars=308 head='BEGINyyy", lines[0])
        self.assertIn("yyyEND'", lines[0])

    def test_a_buffered_stream_must_finish_within_its_budget(self):
        # Rule: a buffered provider's budget covers its whole reply, since nothing reaches the
        # client before the end; a lane still generating past it fails over instead of holding
        # the lane while the client's idle timeout runs out.
        slow = self._upstream(stream_pieces=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
                              finish=("stop", {}), pace=0.6)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "buffer_stream": True,
                       "budget_s": 1},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")

    def test_a_cut_off_reply_is_served_when_no_later_tier_answers(self):
        # Rule: a whole reply from a later tier wins, but a cut-off reply beats none at all:
        # claude-mem stores its complete blocks when told finish_reason=length.
        cut = self._upstream(body={"model": "fake", "choices": [
            {"message": {"content": "<observation>a</observation><obs"},
             "finish_reason": "length"}]})
        down = self._upstream(status=503, body={"error": "down"})
        self._two_geminis(cut, down)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        choice = json.loads(raw)["choices"][0]
        self.assertEqual(choice["message"]["content"], "<observation>a</observation><obs")
        self.assertEqual(choice["finish_reason"], "length")

    def test_a_buffered_cut_off_stream_is_served_when_no_later_tier_answers(self):
        # Rule: as above, on the streaming path; the last frame says finish_reason=length.
        cut = self._upstream(stream_pieces=[{"content": "<observation>a</observation>"},
                                            {"content": "<obs"}],
                             finish=("length", {"prompt_tokens": 9, "completion_tokens": 2048}))
        down = self._upstream(status=503, body={"error": "down"})
        self._buffered_then_fallback(cut, down)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "<observation>a</observation><obs")
        frames = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith("data: {")]
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "length")
        self.assertTrue(raw.decode().rstrip().endswith("data: [DONE]"))

    def test_a_buffered_budget_holds_even_while_each_read_beats_the_read_timeout(self):
        # Rule: the whole-reply budget is a hard bound; a lane dribbling frames just inside the
        # read timeout cannot hold the request past it.
        slow = self._upstream(stream_pieces=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
                              finish=("stop", {}), pace=1.5)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "buffer_stream": True,
                       "budget_s": 2},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        started = time.time()
        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})
        elapsed = time.time() - started

        self.assertEqual(self._streamed_text(raw), "fallback")
        self.assertLess(elapsed, 2.7)  # budget 2 s; the next frame was due at 3 s

    def test_a_buffered_budget_includes_the_wait_for_headers(self):
        # Rule: the whole-reply budget starts when the request is sent, so time spent before the
        # first line (a lane holding its headers through prefill) comes out of it.
        late = self._upstream(stream_pieces=[{"content": "late"}], finish=("stop", {}),
                              delay=0.6, stall=3.0)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": late.url, "model": "lane", "buffer_stream": True,
                       "budget_s": 1},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        started = time.time()
        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})
        elapsed = time.time() - started

        self.assertEqual(self._streamed_text(raw), "fallback")
        self.assertLess(elapsed, 1.4)  # budget 1 s; a fresh read timeout would end at 1.6 s

    def test_an_unbuffered_stream_is_not_cut_at_its_budget_once_committed(self):
        # Rule: the whole-reply bound applies only to buffer_stream; a committed stream that
        # keeps arriving within the read timeout is forwarded whole.
        slow = self._upstream(stream_pieces=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
                              finish=("stop", {}), pace=0.6)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "budget_s": 1},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(self._streamed_text(raw), "abc")
        self.assertEqual(fallback.requests, [])

    def _chars_received(self, upstream: FakeUpstream) -> int:
        return router._message_chars(upstream.requests[0][1]["messages"])

    def test_each_provider_gets_the_prompt_sized_for_it(self):
        # Rule: max_prompt_chars tightens the router-wide prompt guard for one provider; a
        # provider without it gets the full, router-bounded prompt on the same request.
        small = self._upstream(status=503, body={"error": "down"})
        large = self._upstream(body=completion("ok"))
        _use({"small": {"type": "openai", "url": small.url, "model": "s",
                        "max_prompt_chars": 3_000},
              "large": {"type": "openai", "url": large.url, "model": "l"}})
        messages = [{"role": "system", "content": "contract " * 50},
                    {"role": "user", "content": "x" * 10_000}]

        status, _ = self._post({"messages": messages})

        self.assertEqual(status, 200)
        self.assertLessEqual(self._chars_received(small), 3_000)
        self.assertEqual(self._chars_received(large), router._message_chars(messages))
        self.assertIn("contract", json.dumps(small.requests[0][1]["messages"]))  # system kept

    def test_a_group_block_logs_each_members_interval(self):
        # Rule: members with different break_seconds are skipped for different times, and the
        # log line says which.
        router.configure(router.parse_config({
            "chain": ["pool"],
            "providers": {
                "a": {"type": "openai", "url": "http://h/v1", "model": "m", "break_seconds": 300},
                "b": {"type": "openai", "url": "http://h/v1", "model": "m"}},
            "groups": {"pool": {"members": ["a", "b"]}}}))

        with contextlib.redirect_stdout(io.StringIO()) as out:
            router.group_overloaded("pool", "test")

        self.assertIn("a(300s)", out.getvalue())
        self.assertIn(f"b({router.BREAK_SECONDS:.0f}s)", out.getvalue())

    def test_a_provider_prompt_limit_above_the_request_changes_nothing(self):
        # Rule: a limit only acts on a prompt that exceeds it.
        upstream = self._upstream(body=completion("ok"))
        _use({"p": {"type": "openai", "url": upstream.url, "model": "m",
                    "max_prompt_chars": 50_000}})
        messages = [{"role": "user", "content": "x" * 10_000}]

        self._post({"messages": messages})

        self.assertEqual(upstream.requests[0][1]["messages"], messages)

    def test_a_provider_breaker_threshold_overrides_the_router_default(self):
        # Rule: break_after is per provider: one failure skips the strict provider, while the
        # default provider is still tried until it has failed BREAK_AFTER times.
        strict = self._upstream(status=500, body={"error": "x"})
        default = self._upstream(status=500, body={"error": "x"})
        last = self._upstream(body=completion("ok"))
        _use({"strict": {"type": "openai", "url": strict.url, "model": "s", "break_after": 1},
              "default": {"type": "openai", "url": default.url, "model": "d"},
              "last": {"type": "openai", "url": last.url, "model": "l"}})

        for _ in range(2):
            self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual((len(strict.requests), len(default.requests)), (1, 2))

    def test_a_provider_breaker_interval_overrides_the_router_default(self):
        # Rule: break_seconds sets how long that provider is skipped, for its own breaker and
        # when a group block skips it.
        failing = self._upstream(status=500, body={"error": "x"})
        down = self._upstream(status=503, body={"error": "down"})
        last = self._upstream(body=completion("ok"))
        router.configure(router.parse_config({
            "chain": ["own", "pool", "last"],
            "providers": {
                "own": {"type": "openai", "url": failing.url, "model": "o", "break_after": 1,
                        "break_seconds": 600},
                "member": {"type": "gemini", "url": down.url, "model": "m",
                           "api_key_env": "TEST_GEMINI_A", "break_seconds": 300},
                "last": {"type": "openai", "url": last.url, "model": "l"},
            },
            "groups": {"pool": {"members": ["member"]}},
        }))

        self._post({"messages": [{"role": "user", "content": "hi"}]})

        breaker = self._get_health()["breaker"]
        self.assertAlmostEqual(breaker["own"]["skipped_for_s"], 600, delta=5)
        self.assertAlmostEqual(breaker["member"]["skipped_for_s"], 300, delta=5)

    def _pool(self, first: FakeUpstream, second: FakeUpstream, fallback: FakeUpstream) -> None:
        router.configure(router.parse_config({
            "chain": ["pool", "fallback"],
            "providers": {
                "gemini-a": {"type": "gemini", "url": first.url, "model": "model-a",
                             "api_key_env": "TEST_GEMINI_A"},
                "gemini-b": {"type": "gemini", "url": second.url, "model": "model-b",
                             "api_key_env": "TEST_GEMINI_B"},
                "fallback": {"type": "openai", "url": fallback.url, "model": "f"},
            },
            "groups": {"pool": {"members": ["gemini-a", "gemini-b"]}},
        }))

    def test_a_group_spreads_consecutive_requests_across_its_members(self):
        first = self._upstream(body=completion("from a"))
        second = self._upstream(body=completion("from b"))
        fallback = self._upstream(body=completion("fallback"))
        self._pool(first, second, fallback)

        answers = [json.loads(self._post({"messages": [{"role": "user", "content": "hi"}]})[1])
                   ["choices"][0]["message"]["content"] for _ in range(4)]

        self.assertEqual(sorted(answers), ["from a", "from a", "from b", "from b"])
        self.assertEqual(fallback.requests, [])

    def test_a_failing_member_retries_inside_the_group_before_falling_back(self):
        first = self._upstream(status=500, body={"error": "broken key"})
        second = self._upstream(body=completion("from b"))
        fallback = self._upstream(body=completion("fallback"))
        self._pool(first, second, fallback)

        answers = [json.loads(self._post({"messages": [{"role": "user", "content": "hi"}]})[1])
                   ["choices"][0]["message"]["content"] for _ in range(2)]

        self.assertEqual(answers, ["from b", "from b"])
        self.assertEqual(fallback.requests, [])

    def _pool_of(self, members: list[FakeUpstream], fallback: FakeUpstream, budget=12) -> None:
        names = [f"gemini-{index}" for index in range(len(members))]
        router.configure(router.parse_config({
            "chain": ["pool", "fallback"],
            "providers": {
                **{name: {"type": "gemini", "url": upstream.url, "model": name, "budget_s": budget,
                          "api_key_env": "TEST_GEMINI_A"}
                   for name, upstream in zip(names, members)},
                "fallback": {"type": "openai", "url": fallback.url, "model": "f"},
            },
            "groups": {"pool": {"members": names}},
        }))

    def test_an_overloaded_member_blocks_its_whole_group(self):
        overloaded = self._upstream(status=503, body=[{"error": {
            "code": 503, "status": "UNAVAILABLE",
            "message": "This model is currently experiencing high demand."}}])
        siblings = [self._upstream(body=completion("sibling")) for _ in range(2)]
        fallback = self._upstream(body=completion("fallback"))
        self._pool_of([overloaded, *siblings], fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "fallback")
        self.assertEqual([len(s.requests) for s in siblings], [0, 0])
        breaker = self._get_health()["breaker"]
        self.assertTrue(all("overloaded" in breaker[f"gemini-{i}"]["reason"] for i in range(3)),
                        breaker)

    def test_one_timeout_moves_on_to_the_next_member_without_blocking_the_group(self):
        slow = self._upstream(body=completion("late"), delay=3.0)
        sibling = self._upstream(body=completion("sibling"))
        fallback = self._upstream(body=completion("fallback"))
        self._pool_of([slow, sibling], fallback, budget=1)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "sibling")
        self.assertEqual(self._get_health()["breaker"]["gemini-1"]["reason"], "")

    def test_two_members_timing_out_in_a_row_block_the_group(self):
        slow = [self._upstream(body=completion("late"), delay=3.0) for _ in range(2)]
        sibling = self._upstream(body=completion("sibling"))
        fallback = self._upstream(body=completion("fallback"))
        self._pool_of([*slow, sibling], fallback, budget=1)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "fallback")
        self.assertEqual(sibling.requests, [])
        self.assertIn("overloaded", self._get_health()["breaker"]["gemini-2"]["reason"])

    # Gemini's 429 for shared model capacity: no QuotaFailure, no RetryInfo, so no reset to wait for.
    EXHAUSTED = [{"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).",
                            "status": "RESOURCE_EXHAUSTED"}}]

    def test_two_members_exhausted_in_a_row_block_the_group(self):
        # Rule: a 429 that names no quota, from two members in a row, is the model out of
        # capacity rather than one key: every member is skipped for the breaker interval.
        exhausted = [self._upstream(status=429, body=self.EXHAUSTED) for _ in range(2)]
        sibling = self._upstream(body=completion("sibling"))
        fallback = self._upstream(body=completion("fallback"))
        self._pool_of([*exhausted, sibling], fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "fallback")
        self.assertEqual(sibling.requests, [])
        self.assertIn("overloaded", self._get_health()["breaker"]["gemini-2"]["reason"])

    def test_a_buffered_hosted_provider_fails_over_on_a_cut_off_stream(self):
        # Rule: buffer_stream is a per-provider stream policy, not a GPU-lane gate, so a hosted
        # type accepts it too.
        cut = self._upstream(stream_pieces=[{"content": "echo"}],
                             finish=("length", {"completion_tokens": 4096}))
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"gemini-a": {"type": "gemini", "url": cut.url, "model": "m",
                           "api_key_env": "TEST_GEMINI_A", "buffer_stream": True},
              "fallback": {"type": "openai", "url": fallback.url, "model": "f"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(self._streamed_text(raw), "fallback")

    def test_the_group_strike_threshold_is_configurable(self):
        # Rule: OBSERVER_GROUP_STRIKES sets how many members must fail in a row before the group
        # is blocked; with 3, two exhausted members still leave the third to answer.
        settings, errors = build_settings({"OBSERVER_GROUP_STRIKES": "3"})
        self.assertEqual(errors, ())
        self.assertEqual(settings.group_strikes, 3)
        saved = router.GROUP_STRIKES_TO_BLOCK
        router.GROUP_STRIKES_TO_BLOCK = 3
        self.addCleanup(setattr, router, "GROUP_STRIKES_TO_BLOCK", saved)
        exhausted = [self._upstream(status=429, body=self.EXHAUSTED) for _ in range(2)]
        sibling = self._upstream(body=completion("sibling"))
        self._pool_of([*exhausted, sibling], self._upstream(body=completion("fallback")))

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "sibling")

    def test_one_member_exhausted_does_not_block_the_group(self):
        # Rule: a single unexplained 429 can be one key; its siblings are still tried.
        exhausted = self._upstream(status=429, body=self.EXHAUSTED)
        siblings = [self._upstream(body=completion("sibling")) for _ in range(2)]
        fallback = self._upstream(body=completion("fallback"))
        self._pool_of([exhausted, *siblings], fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "sibling")
        self.assertEqual(self._get_health()["breaker"]["gemini-2"]["reason"], "")

    def test_a_stall_after_content_is_committed_does_not_block_the_group(self):
        first = self._upstream(stream_pieces=[{"content": "hel"}], stall=0.0)
        sibling = self._upstream(stream_pieces=[{"content": "sibling"}])
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._pool_of([first, sibling], fallback, budget=1)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertIn("hel", raw.decode())
        self.assertEqual(fallback.requests, [])
        self.assertEqual(self._get_health()["breaker"]["gemini-1"]["reason"], "")

    def test_a_stream_that_stalls_after_its_headers_fails_over(self):
        stalled = self._upstream(stream_pieces=[{"content": "late"}], stall=3.0)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._pool_of([stalled], fallback, budget=1)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertIn("fallback", raw.decode())

    def test_a_quota_429_blocks_only_that_member(self):
        exhausted = self._upstream(status=429, body=json.loads(_gemini_429(
            ["GenerateRequestsPerDayPerProjectPerModel-FreeTier"]))[0])
        sibling = self._upstream(body=completion("sibling"))
        fallback = self._upstream(body=completion("fallback"))
        self._pool_of([exhausted, sibling], fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "sibling")
        self.assertEqual(self._get_health()["breaker"]["gemini-1"]["reason"], "")

    def test_health_reports_groups_and_their_members(self):
        self._pool(self._upstream(body={}), self._upstream(body={}), self._upstream(body={}))

        health = self._get_health()

        self.assertEqual(health["chain"], ["pool", "fallback"])
        self.assertEqual(health["groups"], {"pool": ["gemini-a", "gemini-b"]})
        self.assertEqual(set(health["breaker"]), {"gemini-a", "gemini-b", "fallback"})

    def test_health_shows_a_providers_overrides_only_when_set(self):
        # Rule: after editing the config and restarting, /health confirms what took effect.
        upstream = self._upstream(body={})
        _use({"tuned": {"type": "openai", "url": upstream.url, "model": "m", "max_prompt_chars": 96_000,
                        "break_after": 2, "break_seconds": 30},
              "plain": {"type": "openai", "url": upstream.url, "model": "m"}})

        providers = self._get_health()["providers"]

        self.assertEqual(providers["tuned"], {"type": "openai", "model": "m", "budget_s": 40.0,
                                              "max_prompt_chars": 96_000, "break_after": 2,
                                              "break_seconds": 30.0})
        self.assertEqual(providers["plain"], {"type": "openai", "model": "m", "budget_s": 40.0})

    def test_health_lists_each_instance_in_chain_order(self):
        self._two_geminis(self._upstream(body={}), self._upstream(body={}))

        health = self._get_health()

        self.assertEqual(health["chain"], ["gemini-a", "gemini-b"])
        self.assertEqual(health["providers"]["gemini-b"],
                         {"type": "gemini", "model": "model-b", "budget_s": 18.0})


if __name__ == "__main__":
    unittest.main()
