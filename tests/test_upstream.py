"""The upstream call: retries, overload flags, streams, cut-off replies and HTTP failures."""

import contextlib
import io
import json
import os
import time
import unittest

from observer_router.config import Provider
from observer_router.failure import TierFailure
from observer_router.idle import LaneLocks
from observer_router.upstream import iter_stream, nonstream

from tests.support import FakeUpstream, completion


def _provider(url: str, **changes) -> Provider:
    fields = {"name": "p", "type": "openai", "url": url, "model": "m", "budget": 40.0}
    fields.update(changes)
    return Provider(**fields)


def _gemini_429(quota_ids, retry_delay="34s"):
    return json.dumps([{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
             "quotaId": quota_id} for quota_id in quota_ids]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
    ]}}])


class UpstreamTestCase(unittest.TestCase):
    def setUp(self):
        self.upstreams: list[FakeUpstream] = []

    def tearDown(self):
        for upstream in self.upstreams:
            upstream.close()

    def upstream(self, **kwargs) -> FakeUpstream:
        upstream = FakeUpstream(**kwargs)
        self.upstreams.append(upstream)
        return upstream

    def run_call(self, provider, body=None):
        return nonstream(provider, body or {"messages": []}, deadline=time.time() + 60,
                         lanes=LaneLocks())


class TimeoutTests(UpstreamTestCase):
    """A timeout says something about the provider only when it used the whole budget."""

    def test_a_timeout_shortened_by_the_request_deadline_is_not_an_overload(self):
        slow = self.upstream(body=completion("x"), delay=2.0)
        provider = _provider(slow.url, budget=4.0)

        with self.assertRaises(TierFailure) as caught:
            nonstream(provider, {"messages": []}, deadline=time.time() + 1, lanes=LaneLocks())

        self.assertFalse(caught.exception.overloaded)

    def test_a_full_budget_timeout_is_an_overload(self):
        slow = self.upstream(body=completion("x"), delay=2.0)
        provider = _provider(slow.url, budget=1.0)

        with self.assertRaises(TierFailure) as caught:
            self.run_call(provider)

        self.assertTrue(caught.exception.overloaded)

    def test_an_openrouter_retry_that_times_out_keeps_the_overload_flag(self):
        os.environ["TEST_KEY_A"] = "k"
        self.addCleanup(os.environ.pop, "TEST_KEY_A", None)
        upstream = self.upstream(script=[
            (400, {"error": {"message": "Reasoning is mandatory for this endpoint"}}, 0.0),
            (200, completion("x"), 2.0),
        ])
        provider = _provider(upstream.url, type="openrouter", budget=1.0, api_key_env="TEST_KEY_A")

        with self.assertRaises(TierFailure) as caught:
            self.run_call(provider)

        self.assertTrue(caught.exception.overloaded)


class ReasoningRetryTests(UpstreamTestCase):
    """An OpenRouter endpoint that refuses reasoning-disabled is retried once, excluded."""

    def test_a_mandatory_reasoning_error_is_retried_with_exclude(self):
        os.environ["TEST_KEY_A"] = "k"
        self.addCleanup(os.environ.pop, "TEST_KEY_A", None)
        upstream = self.upstream(script=[
            (400, {"error": {"message": "Reasoning is mandatory for this endpoint"}}, 0.0),
            (200, completion("answered"), 0.0),
        ])
        provider = _provider(upstream.url, type="openrouter", api_key_env="TEST_KEY_A")

        payload, content = self.run_call(provider)

        self.assertEqual(content, "answered")
        self.assertEqual(len(upstream.requests), 2)
        self.assertEqual(upstream.requests[1][1]["reasoning"], {"exclude": True})

    def test_a_plain_400_is_not_retried_and_keeps_the_snippet(self):
        upstream = self.upstream(status=400, body={"error": {"message": "bad request"}})
        provider = _provider(upstream.url)

        with self.assertRaises(TierFailure) as caught:
            self.run_call(provider)

        self.assertEqual(len(upstream.requests), 1)
        self.assertIn("bad request", str(caught.exception))


class StreamTests(UpstreamTestCase):
    """A stream yields the served model once, then content deltas; reasoning is dropped."""

    def frames(self, provider):
        return list(iter_stream(provider, {"messages": [], "stream": True},
                                deadline=time.time() + 60, lanes=LaneLocks()))

    def test_only_content_deltas_are_forwarded(self):
        upstream = self.upstream(stream_pieces=[{"content": "a"}, {"reasoning": "think"},
                                                {"content": "b"}], finish=("stop", {}))

        self.assertEqual(self.frames(_provider(upstream.url)),
                         [("meta", "fake"), ("delta", "a"), ("delta", "b")])

    def test_a_buffered_reply_cut_off_at_the_limit_fails_over(self):
        # Rule: a buffered provider yields nothing until its stream ends, so a cut-off reply
        # can still be replaced by a later tier.
        upstream = self.upstream(stream_pieces=[{"content": "echo"}],
                                 finish=("length", {"completion_tokens": 4096}),)

        with self.assertRaises(TierFailure) as caught:
            self.frames(_provider(upstream.url, buffer_stream=True))

        self.assertEqual(caught.exception.partial, ("fake", "echo"))

    def test_a_stream_that_never_produced_content_fails(self):
        upstream = self.upstream(stream_pieces=[], finish=("stop", {}))

        with self.assertRaises(TierFailure) as caught:
            self.frames(_provider(upstream.url))

        self.assertIn("no content", str(caught.exception))

    def test_a_buffered_stream_without_a_finish_reason_fails(self):
        upstream = self.upstream(stream_pieces=[{"content": "a"}])

        with self.assertRaises(TierFailure) as caught:
            self.frames(_provider(upstream.url, buffer_stream=True))

        self.assertIn("finish_reason", str(caught.exception))


class NonstreamTests(UpstreamTestCase):
    """A non-streaming reply is a result only when it carries content."""

    def test_content_and_payload_are_returned(self):
        upstream = self.upstream(body=completion("hello"))

        payload, content = self.run_call(_provider(upstream.url))

        self.assertEqual(content, "hello")
        self.assertEqual(payload["model"], "fake")

    def test_empty_content_is_a_failure(self):
        upstream = self.upstream(body={"choices": [{"message": {"content": ""},
                                                    "finish_reason": "stop"}]})

        with self.assertRaises(TierFailure) as caught:
            self.run_call(_provider(upstream.url))

        self.assertIn("empty content", str(caught.exception))

    def test_a_reply_cut_off_at_the_limit_is_kept_as_a_partial(self):
        upstream = self.upstream(body={"model": "fake",
                                       "choices": [{"message": {"content": "echoed"},
                                                    "finish_reason": "length"}]})

        with contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(TierFailure) as caught:
            self.run_call(_provider(upstream.url))

        self.assertEqual(caught.exception.partial, ("fake", "echoed"))
        self.assertIn("cut off at the token limit", out.getvalue())


class HttpFailureTests(UpstreamTestCase):
    """An HTTP failure becomes a TierFailure with the quota or overload it names."""

    def test_a_gemini_daily_quota_carries_its_reset(self):
        upstream = self.upstream(status=429, body=json.loads(_gemini_429(
            ["GenerateRequestsPerDayPerProjectPerModel-FreeTier"])))
        provider = _provider(upstream.url, type="gemini", api_key_env="TEST_K")
        os.environ["TEST_K"] = "k"
        self.addCleanup(os.environ.pop, "TEST_K", None)

        with self.assertRaises(TierFailure) as caught:
            self.run_call(provider)

        self.assertTrue(caught.exception.retry_at)
        self.assertIn("quota", str(caught.exception))

    def test_a_503_marks_the_model_unavailable(self):
        upstream = self.upstream(status=503, body={"error": {"message": "busy"}})

        with self.assertRaises(TierFailure) as caught:
            self.run_call(_provider(upstream.url))

        self.assertEqual(caught.exception.overloaded, "unavailable")

    def test_an_unexplained_429_marks_the_model_exhausted(self):
        upstream = self.upstream(status=429, body={"error": {"message": "slow down"}})

        with self.assertRaises(TierFailure) as caught:
            self.run_call(_provider(upstream.url))

        self.assertEqual(caught.exception.overloaded, "exhausted")


if __name__ == "__main__":
    unittest.main()
