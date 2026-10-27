"""The HTTP surface: the host and content-type guards, and failover across the chain."""

import contextlib
import http.client
import io
import json
import os
import socket
import threading
import time
import unittest
import urllib.request
from dataclasses import replace
from urllib.error import HTTPError

from observer_router.prompt import message_chars
from observer_router.server import serve
from tests.support import FakeUpstream, completion, make_runtime, make_settings


REMOTE = {"type": "openai", "url": "http://127.0.0.1:9/v1/chat/completions", "model": "r"}
LANE = {**REMOTE, "model": "lane", "serial": True, "ac_only": True}


def _gemini_429(quota_ids, retry_delay="34s"):
    """A Gemini 429 in the shape the OpenAI-compatible endpoint returned on 2026-09-24."""
    return json.dumps([{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
             "quotaId": quota_id} for quota_id in quota_ids]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
    ]}}])


class ServerTestCase(unittest.TestCase):
    """Starts one router on a free port and swaps its runtime between requests."""

    def setUp(self):
        self.env = dict(os.environ)
        os.environ.update({"TEST_GEMINI_A": "key-a", "TEST_GEMINI_B": "key-b"})
        self.upstreams: list[FakeUpstream] = []
        self.server = serve(make_runtime({"providers": {"placeholder": REMOTE}}),
                            "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for upstream in self.upstreams:
            upstream.close()
        os.environ.clear()
        os.environ.update(self.env)

    def _use(self, tables=None, chain=None, config=None, **settings):
        self.server.runtime = make_runtime(config or {"providers": tables}, chain,
                                           settings=make_settings(**settings))

    def _allow(self, *hosts):
        runtime = self.server.runtime
        self.server.runtime = replace(
            runtime, settings=replace(runtime.settings, allowed_hosts=tuple(hosts)))

    def _upstream(self, **kwargs) -> FakeUpstream:
        upstream = FakeUpstream(**kwargs)
        self.upstreams.append(upstream)
        return upstream


class RequestGuardTests(ServerTestCase):
    """A web page must not be able to drive the router: it takes neither a Host header that a
    rebound DNS name would carry, nor a POST that is not declared JSON."""

    def setUp(self):
        super().setUp()
        self.upstream = self._upstream(body=completion("hi"))
        self._use({"p": {"type": "openai", "url": self.upstream.url, "model": "m"}})
        self.port = self.server.server_port

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
        self._allow("router.lan",)
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

    def test_a_listed_host_name_is_served(self):
        # Rule: OBSERVER_ROUTER_ALLOWED_HOSTS names the DNS names that may reach the router.
        self._allow("router.lan",)
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


class ChainTests(ServerTestCase):
    """Requests through the router's HTTP surface fail over between named instances."""


    def _two_geminis(self, first: FakeUpstream, second: FakeUpstream) -> None:
        self._use({
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
        self._use({
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
        self._use({"lane": {"type": "openai", "url": cut.url, "model": "lane"},
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
        self._use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "buffer_stream": True,
                       "budget_s": 1},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")

    def test_no_tier_starts_once_the_request_deadline_has_passed(self):
        # Rule: a request has one deadline from its arrival; a reply after it reaches no client,
        # so once a slow tier has used it up the chain ends with a 502 instead of trying on.
        slow = self._upstream(body=completion("late"), delay=3)
        fallback = self._upstream(body=completion("fallback"))
        self._use({"slow": {"type": "openai", "url": slow.url, "model": "slow", "budget_s": 10},
                   "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}},
                  request_deadline_seconds=1.5)

        started = time.time()
        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertLess(time.time() - started, 2.5)
        self.assertEqual(status, 502)
        self.assertEqual(fallback.requests, [])
        self.assertIn("deadline", json.loads(raw)["attempted"][1])
        self.assertEqual(self._get_health()["breaker"]["fallback"]["failures"], 0)

    def test_waiting_for_a_busy_serial_lane_counts_against_the_request_deadline(self):
        # Rule: a serial lane held by another request is waited for only while the deadline
        # leaves the provider time to run; then the chain moves on, and the wait is no fault of
        # the provider.
        lane = self._upstream(stream_pieces=[{"content": "lane"}])
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._use({"lane": {**LANE, "url": lane.url, "min_budget_s": 1.5},
                   "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}},
                  request_deadline_seconds=3)
        held = self.server.runtime.lanes.guard(self.server.runtime.providers["lane"])
        held.acquire()
        try:
            started = time.time()
            status, raw = self._post({"messages": [{"role": "user", "content": "hi"}],
                                      "stream": True})
            elapsed = time.time() - started
        finally:
            held.release()

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")
        self.assertEqual(lane.requests, [])
        self.assertGreater(elapsed, 1.2)  # it waited for the lane until 1.5 s were left
        self.assertLess(elapsed, 2.5)
        self.assertEqual(self._get_health()["breaker"]["lane"]["failures"], 0)

    def test_a_provider_is_skipped_when_less_than_its_minimum_budget_is_left(self):
        # Rule: min_budget_s is the least time a provider can answer in; with less of the
        # request deadline left it is not called at all.
        lane = self._upstream(stream_pieces=[{"content": "lane"}])
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._use({"lane": {"type": "openai", "url": lane.url, "model": "lane",
                            "min_budget_s": 5},
                   "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}},
                  request_deadline_seconds=3)

        started = time.time()
        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertLess(time.time() - started, 1.0)
        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")
        self.assertEqual(lane.requests, [])

    def test_a_reply_from_a_rejected_model_fails_over(self):
        # Rule: a provider that routes to models itself (openrouter/free) can land on one that
        # cannot do the job; a reply served by a model on its reject_models list fails over
        # like an empty one, before anything reaches the client.
        for stream in (True, False):
            with self.subTest(stream=stream):
                if stream:
                    routed = self._upstream(stream_pieces=[{"content": "User Safety: safe"}])
                    fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
                else:
                    routed = self._upstream(body=completion("User Safety: safe"))
                    fallback = self._upstream(body=completion("fallback"))
                self._use({"routed": {"type": "openai", "url": routed.url, "model": "auto",
                                      "reject_models": ["fake"]},
                           "fallback": {"type": "openai", "url": fallback.url,
                                        "model": "fallback"}})

                with contextlib.redirect_stdout(io.StringIO()) as out:
                    status, raw = self._post({"messages": [{"role": "user", "content": "hi"}],
                                              "stream": stream})

                self.assertEqual(status, 200)
                text = (self._streamed_text(raw) if stream
                        else json.loads(raw)["choices"][0]["message"]["content"])
                self.assertEqual(text, "fallback")
                self.assertEqual(len(routed.requests), 1)
                self.assertIn("tier routed failed: routed: served by rejected model fake",
                              out.getvalue())

    def test_a_served_line_reports_the_requests_age(self):
        # Rule: latency counts from the answering tier's start; age counts from the request's
        # arrival, which is what a client's timeout measures.
        upstream = self._upstream(stream_pieces=[{"content": "hi"}])
        self._use({"x": {"type": "openai", "url": upstream.url, "model": "x"}})

        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertRegex(out.getvalue(),
                         r"served tier=x model=fake latency=[\d.]+s age=[\d.]+s stream=1")

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
        self._use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "buffer_stream": True,
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
        self._use({"lane": {"type": "openai", "url": late.url, "model": "lane", "buffer_stream": True,
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
        self._use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "budget_s": 1},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(self._streamed_text(raw), "abc")
        self.assertEqual(fallback.requests, [])

    def _chars_received(self, upstream: FakeUpstream) -> int:
        return message_chars(upstream.requests[0][1]["messages"])

    def test_each_provider_gets_the_prompt_sized_for_it(self):
        # Rule: max_prompt_chars tightens the router-wide prompt guard for one provider; a
        # provider without it gets the full, router-bounded prompt on the same request.
        small = self._upstream(status=503, body={"error": "down"})
        large = self._upstream(body=completion("ok"))
        self._use({"small": {"type": "openai", "url": small.url, "model": "s",
                        "max_prompt_chars": 3_000},
              "large": {"type": "openai", "url": large.url, "model": "l"}})
        messages = [{"role": "system", "content": "contract " * 50},
                    {"role": "user", "content": "x" * 10_000}]

        status, _ = self._post({"messages": messages})

        self.assertEqual(status, 200)
        self.assertLessEqual(self._chars_received(small), 3_000)
        self.assertEqual(self._chars_received(large), message_chars(messages))
        self.assertIn("contract", json.dumps(small.requests[0][1]["messages"]))  # system kept

    def test_a_group_block_logs_each_members_interval(self):
        # Rule: members with different break_seconds are skipped for different times, and the
        # log line says which.
        self._use(config={
            "chain": ["pool"],
            "providers": {
                "a": {"type": "openai", "url": "http://h/v1", "model": "m", "break_seconds": 300},
                "b": {"type": "openai", "url": "http://h/v1", "model": "m"}},
            "groups": {"pool": {"members": ["a", "b"]}}})
        runtime = self.server.runtime

        with contextlib.redirect_stdout(io.StringIO()) as out:
            runtime.breakers.group_overloaded("pool", "test")

        self.assertIn("a(300s)", out.getvalue())
        self.assertIn(f"b({runtime.settings.break_seconds:.0f}s)", out.getvalue())

    def test_a_provider_prompt_limit_above_the_request_changes_nothing(self):
        # Rule: a limit only acts on a prompt that exceeds it.
        upstream = self._upstream(body=completion("ok"))
        self._use({"p": {"type": "openai", "url": upstream.url, "model": "m",
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
        self._use({"strict": {"type": "openai", "url": strict.url, "model": "s", "break_after": 1},
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
        self._use(config={
            "chain": ["own", "pool", "last"],
            "providers": {
                "own": {"type": "openai", "url": failing.url, "model": "o", "break_after": 1,
                        "break_seconds": 600},
                "member": {"type": "gemini", "url": down.url, "model": "m",
                           "api_key_env": "TEST_GEMINI_A", "break_seconds": 300},
                "last": {"type": "openai", "url": last.url, "model": "l"},
            },
            "groups": {"pool": {"members": ["member"]}},
        })

        self._post({"messages": [{"role": "user", "content": "hi"}]})

        breaker = self._get_health()["breaker"]
        self.assertAlmostEqual(breaker["own"]["skipped_for_s"], 600, delta=5)
        self.assertAlmostEqual(breaker["member"]["skipped_for_s"], 300, delta=5)

    def _pool(self, first: FakeUpstream, second: FakeUpstream, fallback: FakeUpstream) -> None:
        self._use(config={
            "chain": ["pool", "fallback"],
            "providers": {
                "gemini-a": {"type": "gemini", "url": first.url, "model": "model-a",
                             "api_key_env": "TEST_GEMINI_A"},
                "gemini-b": {"type": "gemini", "url": second.url, "model": "model-b",
                             "api_key_env": "TEST_GEMINI_B"},
                "fallback": {"type": "openai", "url": fallback.url, "model": "f"},
            },
            "groups": {"pool": {"members": ["gemini-a", "gemini-b"]}},
        })

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

    def _pool_of(self, members: list[FakeUpstream], fallback: FakeUpstream, budget=12,
                 **settings) -> None:
        names = [f"gemini-{index}" for index in range(len(members))]
        self._use(config={
            "chain": ["pool", "fallback"],
            "providers": {
                **{name: {"type": "gemini", "url": upstream.url, "model": name, "budget_s": budget,
                          "api_key_env": "TEST_GEMINI_A"}
                   for name, upstream in zip(names, members)},
                "fallback": {"type": "openai", "url": fallback.url, "model": "f"},
            },
            "groups": {"pool": {"members": names}},
        }, **settings)

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
        self._use({"gemini-a": {"type": "gemini", "url": cut.url, "model": "m",
                           "api_key_env": "TEST_GEMINI_A", "buffer_stream": True},
              "fallback": {"type": "openai", "url": fallback.url, "model": "f"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(self._streamed_text(raw), "fallback")

    def test_the_group_strike_threshold_is_configurable(self):
        # Rule: OBSERVER_GROUP_STRIKES sets how many members must fail in a row before the group
        # is blocked; with 3, two exhausted members still leave the third to answer.
        exhausted = [self._upstream(status=429, body=self.EXHAUSTED) for _ in range(2)]
        sibling = self._upstream(body=completion("sibling"))
        self._pool_of([*exhausted, sibling], self._upstream(body=completion("fallback")),
                      group_strikes=3)

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
        self._use({"tuned": {"type": "openai", "url": upstream.url, "model": "m", "max_prompt_chars": 96_000,
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
