"""The runtime: the chain, its rotation, the gates and the failure recording."""

import time
import unittest

from observer_router.failure import TierFailure
from tests.support import make_runtime, make_settings


REMOTE = {"type": "openai", "url": "http://127.0.0.1:9/v1/chat/completions", "model": "r"}
LANE = {**REMOTE, "model": "lane", "serial": True, "ac_only": True}
POOL = {
    "chain": ["pool", "local"],
    "providers": {
        "a": {"type": "openai", "url": "http://127.0.0.1:9/a", "model": "m"},
        "b": {"type": "openai", "url": "http://127.0.0.1:9/b", "model": "m"},
        "c": {"type": "openai", "url": "http://127.0.0.1:9/c", "model": "m"},
        "local": {"type": "openai", "url": "http://127.0.0.1:9/local", "model": "m"},
    },
    "groups": {"pool": {"members": ["a", "b", "c"]}},
}


class RotationTests(unittest.TestCase):
    """A group starts each request one member further on, so its keys share the load."""

    def test_a_group_rotates_its_members_across_requests(self):
        runtime = make_runtime(POOL)

        orders = [runtime.expand_chain() for _ in range(4)]

        self.assertEqual(runtime.chain, ("pool", "local"))
        self.assertEqual(runtime.groups["pool"], ("a", "b", "c"))
        self.assertEqual(orders, [
            ["a", "b", "c", "local"],
            ["b", "c", "a", "local"],
            ["c", "a", "b", "local"],
            ["a", "b", "c", "local"],
        ])

    def test_a_chain_override_replaces_the_configured_order(self):
        runtime = make_runtime({"providers": {"x": POOL["providers"]["local"]}},
                               chain=["x"])

        self.assertEqual(runtime.chain, ("x",))
        self.assertEqual(runtime.expand_chain(), ["x"])


class PowerGateTests(unittest.TestCase):
    """An ac_only lane is left out while on battery; the gate's probe is pinned."""

    def runtime(self, source: str, tables=None):
        return make_runtime({"providers": tables or {"remote": REMOTE, "local": LANE}},
                            probe=lambda: source)

    def test_ac_only_provider_runs_on_ac_power(self):
        allowed, skipped = self.runtime("AC Power").allowed_tiers()

        self.assertEqual(allowed, ["remote", "local"])
        self.assertEqual(skipped, {})

    def test_ac_only_provider_is_skipped_on_battery_with_a_reason(self):
        allowed, skipped = self.runtime("Battery Power").allowed_tiers()

        self.assertEqual(allowed, ["remote"])
        self.assertIn("battery", skipped["local"])

    def test_unreadable_power_state_keeps_the_provider(self):
        allowed, _ = self.runtime("unknown").allowed_tiers()

        self.assertIn("local", allowed)

    def test_a_provider_without_ac_only_runs_on_battery(self):
        allowed, skipped = self.runtime(
            "Battery Power", {"remote": REMOTE, "local": {**LANE, "ac_only": False}}
        ).allowed_tiers()

        self.assertEqual(allowed, ["remote", "local"])
        self.assertEqual(skipped, {})

    def test_health_reports_the_gate(self):
        power = self.runtime("Battery Power").health()["power"]

        self.assertEqual(power["source"], "Battery Power")
        self.assertEqual(power["ac_only"], ["local"])
        self.assertFalse(power["ac_only_allowed"])


class IdleGateTests(unittest.TestCase):
    """A provider with idle_metrics waits until every watched lane has been idle long enough."""

    URL = "http://127.0.0.1:1240/metrics"

    def runtime(self, chain=None, tables=None):
        return make_runtime(
            {"providers": tables or {
                "remote": REMOTE,
                "local": {**LANE, "ac_only": False, "idle_metrics": [self.URL],
                          "idle_seconds": 30}}},
            chain)

    def test_busy_lane_keeps_the_provider_out_with_a_reason(self):
        runtime = self.runtime()
        runtime.idle.record(self.URL, (1011, 1), time.time())

        allowed, skipped = runtime.allowed_tiers()

        self.assertEqual(allowed, ["remote"])
        self.assertIn("lanes busy (127.0.0.1:1240", skipped["local"])
        self.assertFalse(runtime.health()["idle_gate"]["local"]["allowed"])

    def test_the_sampler_watches_only_lanes_of_providers_in_the_chain(self):
        runtime = self.runtime()

        self.assertEqual(runtime.watched_lanes(), (self.URL,))
        self.assertEqual(self.runtime(chain=["remote"]).watched_lanes(), ())

    def test_gate_is_off_without_watched_lanes(self):
        runtime = make_runtime({"providers": {"local": LANE}})

        self.assertIsNone(runtime.busy_reason(runtime.providers["local"]))


class BreakerTests(unittest.TestCase):
    """A recorded failure blocks a tier at its quota reset or its breaker threshold."""

    def runtime(self, **settings):
        return make_runtime({"providers": {"gemini": {**REMOTE, "type": "gemini",
                                                      "api_key_env": "K"}}},
                            settings=make_settings(**settings))

    def test_a_quota_failure_blocks_the_tier_with_its_reason(self):
        runtime = self.runtime()
        until = time.time() + 3600

        runtime.record_failure("gemini", TierFailure("x", until, "PerDayQuota"))

        self.assertIn("quota PerDayQuota until", runtime.skip_reason("gemini") or "")
        self.assertFalse(runtime.available("gemini"))
        self.assertIn("quota PerDayQuota", runtime.health()["breaker"]["gemini"]["reason"])

    def test_a_breaker_cannot_shorten_a_quota_block(self):
        runtime = self.runtime()
        runtime.record_failure("gemini", TierFailure("x", time.time() + 3600, "PerDayQuota"))
        for _ in range(runtime.settings.break_after):
            runtime.record_failure("gemini", TierFailure("gemini: HTTP 503"))

        self.assertIn("quota", runtime.skip_reason("gemini") or "")

    def test_a_success_clears_the_block(self):
        runtime = self.runtime()
        runtime.record_failure("gemini", TierFailure("x", time.time() + 60, "q"))

        runtime.ok("gemini")

        self.assertIsNone(runtime.skip_reason("gemini"))

    def test_repeated_failures_open_the_breaker(self):
        runtime = self.runtime(break_after=2)

        for _ in range(2):
            runtime.record_failure("gemini", TierFailure("gemini: refused"))

        self.assertEqual(runtime.skip_reason("gemini"), "breaker-open")


class IsolationTests(unittest.TestCase):
    """Two runtimes share nothing: a test can build its own and leave no state behind."""

    def test_rotation_and_breakers_do_not_cross_runtimes(self):
        first = make_runtime(POOL, settings=make_settings(break_after=1))
        second = make_runtime(POOL, settings=make_settings(break_after=1))

        first.expand_chain()
        first.record_failure("local", TierFailure("local: refused"))

        self.assertEqual(second.expand_chain(), ["a", "b", "c", "local"])
        self.assertIsNone(second.skip_reason("local"))
        self.assertIsNotNone(first.skip_reason("local"))

    def test_the_health_payload_describes_only_this_runtime(self):
        health = make_runtime({"providers": {"p": REMOTE}}).health()

        self.assertEqual(health["chain"], ["p"])
        self.assertEqual(list(health["providers"]), ["p"])


if __name__ == "__main__":
    unittest.main()
