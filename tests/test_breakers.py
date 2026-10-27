"""Breakers, quota-reset parsing and group strikes."""

import json
import unittest

from observer_router.breakers import Breakers, quota_reset
from observer_router.config import Provider


NOW = 1790234905.0  # 2026-09-24 07:28:25 UTC = 00:28:25 PDT


def _provider(name, **changes) -> Provider:
    fields = {"name": name, "type": "openai", "url": f"http://127.0.0.1:9/{name}",
              "model": "m", "budget": 40.0}
    fields.update(changes)
    return Provider(**fields)


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


class QuotaResetTests(unittest.TestCase):
    """A 429 that states its reset blocks the tier until then instead of re-probing it."""

    def test_gemini_daily_quota_blocks_until_pacific_midnight(self):
        body = _gemini_429([
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        ])

        retry_at, quota = quota_reset("gemini", 429, None, body, now=NOW)

        self.assertEqual(quota, "GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        self.assertEqual(retry_at, 1790319600.0)  # 2026-09-25 00:00 PDT

    def test_gemini_per_minute_quota_uses_retry_delay(self):
        body = _gemini_429(["GenerateRequestsPerMinutePerProjectPerModel-FreeTier"],
                           retry_delay="34.94s")

        retry_at, quota = quota_reset("gemini", 429, None, body, now=NOW)

        self.assertAlmostEqual(retry_at, NOW + 34.94)
        self.assertEqual(quota, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")

    def test_openrouter_reset_header_is_epoch_milliseconds(self):
        body = json.dumps({"error": {"code": 429, "metadata": {
            "headers": {"X-RateLimit-Reset": "1790294400000"},
            "limit_source": "openrouter_free_tier_daily",
        }}})

        retry_at, quota = quota_reset("openrouter", 429,
                                      {"X-RateLimit-Reset": "1790294400000"}, body, now=NOW)

        self.assertEqual(retry_at, 1790294400.0)  # 2026-09-25 00:00 UTC
        self.assertEqual(quota, "openrouter_free_tier_daily")

    def test_openrouter_reads_the_reset_from_the_body_copy(self):
        body = json.dumps({"error": {"metadata": {
            "headers": {"X-RateLimit-Reset": "1790294400000"}}}})

        retry_at, _ = quota_reset("openrouter", 429, None, body, now=NOW)

        self.assertEqual(retry_at, 1790294400.0)

    def test_a_reset_further_out_than_a_day_is_not_trusted(self):
        # Rule: a malformed header must not take a tier offline for days.
        body = json.dumps({"error": {"metadata": {
            "headers": {"X-RateLimit-Reset": "1900000000000"}}}})

        self.assertIsNone(quota_reset("openrouter", 429, None, body, now=NOW))

    def test_a_status_other_than_429_states_no_quota(self):
        self.assertIsNone(quota_reset("gemini", 400, None,
                                      _gemini_429(["GenerateRequestsPerDayPerProjectPerModel"]),
                                      now=NOW))

    def test_an_unreadable_body_states_no_quota(self):
        self.assertIsNone(quota_reset("gemini", 429, None, "not json", now=NOW))

    def test_an_unreadable_or_past_reset_is_not_trusted(self):
        # Rule: a header that is not a number, or names an epoch already passed, states no
        # usable reset.
        for header in ("garbage", "1000"):
            with self.subTest(header):
                self.assertIsNone(quota_reset("openrouter", 429,
                                              {"X-RateLimit-Reset": header}, "{}", now=NOW))


class BreakerTests(unittest.TestCase):
    """A tier that fails repeatedly is skipped briefly instead of spending every call on it."""

    def breaker(self, log=None, **providers) -> Breakers:
        tables = {name: _provider(name, **fields) for name, fields in providers.items()}
        return Breakers(groups={}, providers=tables, break_after=3, break_seconds=60.0,
                        strikes=2, log=log or (lambda message: None),
                        clock=lambda: 1_000.0)

    def test_a_tier_is_skipped_only_after_the_threshold(self):
        breakers = self.breaker()

        self.assertIsNone(breakers.skip_reason("a", now=1_000.0))
        breakers.failed("a", "boom")
        breakers.failed("a", "boom")
        self.assertIsNone(breakers.skip_reason("a", now=1_000.0))
        breakers.failed("a", "boom")

        self.assertEqual(breakers.skip_reason("a", now=1_000.0), "breaker-open")
        self.assertEqual(breakers.skip_reason("a", now=1_060.0), None)

    def test_a_provider_can_override_both_limits(self):
        breakers = self.breaker(a={"break_after": 1, "break_seconds": 5.0})

        breakers.failed("a", "boom")

        self.assertEqual(breakers.skip_reason("a", now=1_000.0), "breaker-open")
        self.assertEqual(breakers.skip_reason("a", now=1_005.0), None)

    def test_a_quota_block_is_not_shortened_by_a_later_failure(self):
        breakers = self.breaker()

        breakers.quota_exhausted("a", 2_000.0, "Daily")
        breakers.failed("a", "boom")
        breakers.failed("a", "boom")
        breakers.failed("a", "boom")

        self.assertIn("quota Daily until", breakers.skip_reason("a", now=1_000.0))

    def test_a_success_clears_the_failures_and_the_skip(self):
        breakers = self.breaker()
        for _ in range(3):
            breakers.failed("a", "boom")

        breakers.ok("a")

        self.assertIsNone(breakers.skip_reason("a", now=1_000.0))
        self.assertEqual(breakers.snapshot(["a"], now=1_000.0)["a"]["failures"], 0)

    def test_the_snapshot_reports_failures_and_the_time_left(self):
        breakers = self.breaker()
        breakers.quota_exhausted("a", 1_030.0, "Daily")

        self.assertEqual(breakers.snapshot(["a"], now=1_000.0),
                         {"a": {"failures": 1, "skipped_for_s": 30.0,
                                "reason": breakers.skip_reason("a", now=1_000.0)}})

    def test_group_strikes_block_the_group_at_the_threshold(self):
        breakers = Breakers(groups={"pool": ("a", "b")},
                            providers={"a": _provider("a"), "b": _provider("b")},
                            break_after=3, break_seconds=60.0, strikes=2,
                            log=lambda message: None)

        self.assertFalse(breakers.strike("pool"))
        self.assertTrue(breakers.strike("pool"))
        self.assertFalse(breakers.strike("pool"))  # the block resets the count

    def test_a_group_block_skips_each_member_for_its_own_interval(self):
        lines = []
        breakers = Breakers(
            groups={"pool": ("a", "b")},
            providers={"a": _provider("a", break_seconds=300.0), "b": _provider("b")},
            break_after=3, break_seconds=60.0, strikes=2, log=lines.append)

        breakers.group_overloaded("pool", "model exhausted", now=1_000.0)

        self.assertEqual(breakers.skip_reason("a", now=1_001.0),
                         "group pool overloaded (model exhausted)")
        self.assertTrue(lines and "a(300s)" in lines[0] and "b(60s)" in lines[0])

    def test_a_tier_is_in_at_most_one_group(self):
        breakers = Breakers(groups={"pool": ("a", "b")}, providers={}, break_after=3,
                            break_seconds=60.0, strikes=2, log=lambda message: None)

        self.assertEqual(breakers.group_of("a"), "pool")
        self.assertIsNone(breakers.group_of("c"))


if __name__ == "__main__":
    unittest.main()
