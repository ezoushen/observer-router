#!/usr/bin/env python3
"""Contract tests for observer-router prompt compaction."""

import importlib.util
import unittest
from pathlib import Path
from typing import Any


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


if __name__ == "__main__":
    unittest.main()
