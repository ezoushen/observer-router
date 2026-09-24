#!/usr/bin/env python3
"""Contract tests for observer-router: prompt compaction, the power and idle gates, the cursor
tier, and quota-reset blocks."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from email.message import Message
from io import BytesIO
from typing import Any
from urllib.error import HTTPError


MODULE_PATH = Path(__file__).with_name("observer-router.py")
SPEC = importlib.util.spec_from_file_location("observer_router", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {MODULE_PATH}")
router: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


class PromptCompactionTests(unittest.TestCase):
    def setUp(self):
        self.total_limit = router.MAX_PROMPT_CHARS
        self.field_limit = router.MAX_FIELD_CHARS
        router.MAX_PROMPT_CHARS = 1_000
        router.MAX_FIELD_CHARS = 400

    def tearDown(self):
        router.MAX_PROMPT_CHARS = self.total_limit
        router.MAX_FIELD_CHARS = self.field_limit

    def test_small_messages_are_unchanged(self):
        messages = [
            {"role": "system", "content": "Observe accurately."},
            {"role": "user", "content": "Summarize this session."},
        ]

        compacted, stats = router.compact_messages(messages)

        self.assertEqual(compacted, messages)
        self.assertFalse(stats["changed"])

    def test_image_parts_and_base64_blobs_are_removed(self):
        blob = "A" * 20_000
        messages = [{
            "role": "tool",
            "content": [
                {"type": "text", "text": f"before {blob} after"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }]

        compacted, stats = router.compact_messages(messages)
        content = compacted[0]["content"]

        self.assertIn("before", content)
        self.assertIn("after", content)
        self.assertIn("binary payload omitted", content)
        self.assertIn("image omitted", content)
        self.assertNotIn(blob, content)
        self.assertGreater(stats["removed_blobs"], 0)
        self.assertEqual(stats["removed_image_parts"], 1)

    def test_oversized_history_keeps_system_and_newest_context(self):
        messages = [
            {"role": "system", "content": "S" * 300},
            {"role": "user", "content": "old-" + "x" * 500},
            {"role": "assistant", "content": "middle-" + "y" * 500},
            {"role": "user", "content": "newest-" + "z" * 500},
        ]

        compacted, stats = router.compact_messages(messages)
        combined = "".join(str(message["content"]) for message in compacted)

        self.assertLessEqual(router._message_chars(compacted), router.MAX_PROMPT_CHARS)
        self.assertEqual(compacted[0]["role"], "system")
        self.assertIn("newest-", combined)
        self.assertNotIn("old-", combined)
        self.assertTrue(stats["changed"])
        self.assertGreater(stats["dropped_messages"], 0)


class PowerGateTests(unittest.TestCase):
    """The local tier is a GPU workload; on battery the router must leave it out.

    power_source() is exercised through its cache so the tests never shell out to pmset.
    """

    def setUp(self):
        self.ac_only = router.LOCAL_AC_ONLY
        router.LOCAL_AC_ONLY = True

    def tearDown(self):
        router.LOCAL_AC_ONLY = self.ac_only
        router._power_cache["checked_at"] = 0.0
        router._power_cache["source"] = "unknown"

    def _pin_power(self, source: str) -> None:
        router._power_cache["checked_at"] = router.time.time()
        router._power_cache["source"] = source

    def test_local_tier_runs_on_ac_power(self):
        self._pin_power("AC Power")

        allowed, skipped = router.allowed_tiers()

        self.assertIn("local", allowed)
        self.assertEqual(skipped, {})

    def test_local_tier_is_skipped_on_battery_with_a_reason(self):
        self._pin_power("Battery Power")

        allowed, skipped = router.allowed_tiers()

        self.assertNotIn("local", allowed)
        self.assertIn("local", skipped)
        self.assertIn("battery", skipped["local"])
        self.assertEqual(allowed, [tier for tier in router.CHAIN if tier != "local"])

    def test_unreadable_power_state_keeps_the_local_tier(self):
        self._pin_power("unknown")

        allowed, _ = router.allowed_tiers()

        self.assertIn("local", allowed)

    def test_disabling_ac_only_allows_local_on_battery(self):
        router.LOCAL_AC_ONLY = False
        self._pin_power("Battery Power")

        allowed, skipped = router.allowed_tiers()

        self.assertIn("local", allowed)
        self.assertEqual(skipped, {})

    def test_snapshot_reports_the_gate(self):
        self._pin_power("Battery Power")

        snapshot = router.power_snapshot()

        self.assertEqual(snapshot["source"], "Battery Power")
        self.assertTrue(snapshot["local_ac_only"])
        self.assertFalse(snapshot["local_tier_allowed"])

    def test_cursor_tier_runs_on_battery(self):
        chain = router.CHAIN
        router.CHAIN = ("cursor", "local")
        try:
            self._pin_power("Battery Power")

            allowed, skipped = router.allowed_tiers()

            self.assertEqual(allowed, ["cursor"])
            self.assertEqual(list(skipped), ["local"])
        finally:
            router.CHAIN = chain


class CursorTierTests(unittest.TestCase):
    """The cursor tier authenticates to a local bridge whose key lives in the bridge's env file."""

    def setUp(self):
        self.key = router.CURSOR_API_KEY
        self.env_file = router.CURSOR_ENV_FILE
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        router.CURSOR_API_KEY = self.key
        router.CURSOR_ENV_FILE = self.env_file
        self.tmp.cleanup()

    def _env_file(self, text: str) -> str:
        path = Path(self.tmp.name) / "cursor-proxy.cfg"
        path.write_text(text)
        return str(path)

    def test_key_is_read_from_the_bridge_env_file(self):
        router.CURSOR_API_KEY = ""
        router.CURSOR_ENV_FILE = self._env_file(
            "# comment\nCURSOR_BRIDGE_PORT=8765\nCURSOR_BRIDGE_API_KEY=\"abc123\"\n"
        )

        headers = router._headers_for("cursor")

        self.assertEqual(headers["Authorization"], "Bearer abc123")

    def test_explicit_key_wins_over_the_env_file(self):
        router.CURSOR_API_KEY = "explicit"
        router.CURSOR_ENV_FILE = self._env_file("CURSOR_BRIDGE_API_KEY=from-file\n")

        self.assertEqual(router._headers_for("cursor")["Authorization"], "Bearer explicit")

    def test_missing_key_fails_the_tier(self):
        router.CURSOR_API_KEY = ""
        router.CURSOR_ENV_FILE = str(Path(self.tmp.name) / "absent.cfg")

        with self.assertRaises(router.TierFailure):
            router._headers_for("cursor")

    def test_payload_uses_the_cursor_model_without_reasoning_fields(self):
        payload = router._payload_for(
            "cursor", {"messages": [{"role": "user", "content": "x"}], "max_tokens": 5}, None
        )

        self.assertEqual(payload["model"], router.CURSOR_MODEL)
        self.assertEqual(payload["max_tokens"], 5)
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_cursor_tier_does_not_take_the_local_lane_lock(self):
        self.assertIsNot(router.local_lane_guard("cursor"), router._LOCAL_LANE_LOCK)


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


def _http_error(code, body, headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return HTTPError("https://example.invalid", code, "error", message, BytesIO(body.encode()))


class QuotaResetTests(unittest.TestCase):
    """A 429 that states its reset blocks the tier until then instead of re-probing it."""

    # 2026-09-24 07:28:25 UTC = 00:28:25 PDT
    NOW = 1790234905.0

    def setUp(self):
        with router._breaker_lock:
            self.saved = (dict(router._failures), dict(router._skip_until),
                          dict(router._skip_reason))

    def tearDown(self):
        with router._breaker_lock:
            for target, saved in zip(
                (router._failures, router._skip_until, router._skip_reason), self.saved
            ):
                target.clear()
                target.update(saved)

    def test_gemini_daily_quota_blocks_until_pacific_midnight(self):
        error = _http_error(429, _gemini_429([
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        ]))

        retry_at, quota = router.quota_reset("gemini", error, now=self.NOW)

        self.assertEqual(quota, "GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        self.assertEqual(retry_at, 1790319600.0)  # 2026-09-25 00:00 PDT

    def test_gemini_per_minute_quota_uses_retry_delay(self):
        error = _http_error(429, _gemini_429(
            ["GenerateRequestsPerMinutePerProjectPerModel-FreeTier"], retry_delay="34.94s"
        ))

        retry_at, quota = router.quota_reset("gemini", error, now=self.NOW)

        self.assertAlmostEqual(retry_at, self.NOW + 34.94)
        self.assertEqual(quota, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")

    def test_openrouter_reset_header_is_epoch_milliseconds(self):
        body = json.dumps({"error": {"code": 429, "metadata": {
            "headers": {"X-RateLimit-Reset": "1790294400000"},
            "limit_source": "openrouter_free_tier_daily",
        }}})
        error = _http_error(429, body, {"X-RateLimit-Reset": "1790294400000"})

        retry_at, quota = router.quota_reset("openrouter", error, now=self.NOW)

        self.assertEqual(retry_at, 1790294400.0)  # 2026-09-25 00:00 UTC
        self.assertEqual(quota, "openrouter_free_tier_daily")

    def test_openrouter_reset_is_read_from_the_body_when_headers_lack_it(self):
        body = json.dumps(
            {"error": {"metadata": {"headers": {"X-RateLimit-Reset": "1790294400000"}}}}
        )

        retry_at, _ = router.quota_reset("openrouter", _http_error(429, body), now=self.NOW)

        self.assertEqual(retry_at, 1790294400.0)

    def test_unreadable_or_implausible_resets_fall_back_to_the_breaker(self):
        thirty_days_out = str(int(self.NOW * 1000) + 30 * 86_400_000)
        cases = [
            ("gemini", _http_error(429, "not json")),
            ("gemini", _http_error(500, _gemini_429(["GenerateRequestsPerDayPerProjectPerModel"]))),
            ("openrouter", _http_error(429, "{}", {"X-RateLimit-Reset": "garbage"})),
            ("openrouter", _http_error(429, "{}", {"X-RateLimit-Reset": "1000"})),  # past
            ("openrouter", _http_error(429, "{}", {"X-RateLimit-Reset": thirty_days_out})),
            ("cursor", _http_error(429, _gemini_429(["GenerateRequestsPerDayPerProjectPerModel"]))),
        ]
        for tier, error in cases:
            with self.subTest(tier=tier, code=error.code):
                self.assertIsNone(router.quota_reset(tier, error, now=self.NOW))

    def test_error_body_survives_repeated_reads(self):
        error = _http_error(400, "Reasoning is mandatory for this endpoint")

        self.assertTrue(router._is_mandatory_reasoning_error(error))
        self.assertIn("mandatory", router._snippet(error))

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

        self.assertEqual(router._skip_until["gemini"], until)
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


class IdleGateTests(unittest.TestCase):
    """The local tier waits until every watched interactive lane has been idle long enough."""

    URL = "http://127.0.0.1:1240/metrics"

    def setUp(self):
        self.saved = (router.LOCAL_IDLE_METRICS, router.LOCAL_IDLE_SECONDS, router.LOCAL_AC_ONLY)
        router.LOCAL_IDLE_METRICS = (self.URL,)
        router.LOCAL_IDLE_SECONDS = 30.0
        router.LOCAL_AC_ONLY = False
        router._lane_activity.clear()

    def tearDown(self):
        router.LOCAL_IDLE_METRICS, router.LOCAL_IDLE_SECONDS, router.LOCAL_AC_ONLY = self.saved
        router._lane_activity.clear()

    def _sample(self, submitted, completed, now):
        text = SPLASH_METRICS.format(submitted=submitted, completed=completed)
        router.record_lane_sample(self.URL, router.parse_lane_metrics(text), now)

    def test_in_flight_is_submitted_minus_finished(self):
        text = SPLASH_METRICS.format(submitted=1011, completed=1000)

        self.assertEqual(router.parse_lane_metrics(text), (1011, 1))

    def test_missing_counters_are_unreadable(self):
        self.assertIsNone(router.parse_lane_metrics("splash_requests_submitted_total 3\n"))

    def test_a_lane_never_seen_active_is_idle(self):
        self._sample(1010, 1000, now=1_000.0)

        self.assertEqual(router.busy_lanes(now=1_000.0), [])

    def test_an_in_flight_request_makes_the_lane_busy_for_the_grace_period(self):
        self._sample(1011, 1000, now=1_000.0)

        self.assertEqual(router.busy_lanes(now=1_029.0), [self.URL])
        self.assertEqual(router.busy_lanes(now=1_030.0), [])

    def test_a_request_finished_between_samples_still_counts_as_activity(self):
        self._sample(1010, 1000, now=1_000.0)
        self._sample(1012, 1002, now=1_002.0)  # two requests came and went

        self.assertEqual(router.busy_lanes(now=1_020.0), [self.URL])

    def test_an_unreadable_lane_ages_into_idle(self):
        self._sample(1011, 1000, now=1_000.0)
        router.record_lane_sample(self.URL, None, now=1_010.0)

        self.assertEqual(router.busy_lanes(now=1_031.0), [])

    def test_busy_lane_keeps_the_local_tier_out_with_a_reason(self):
        chain = router.CHAIN
        router.CHAIN = ("cursor", "local")
        try:
            self._sample(1011, 1000, now=router.time.time())

            allowed, skipped = router.allowed_tiers()

            self.assertEqual(allowed, ["cursor"])
            self.assertIn("lanes busy (127.0.0.1:1240", skipped["local"])
            self.assertTrue(router.power_snapshot()["local_tier_allowed"])
            self.assertFalse(router.idle_snapshot()["local_tier_allowed"])
        finally:
            router.CHAIN = chain

    def test_gate_is_off_without_watched_lanes(self):
        router.LOCAL_IDLE_METRICS = ()

        self.assertIsNone(router.local_busy_reason())


if __name__ == "__main__":
    unittest.main()
