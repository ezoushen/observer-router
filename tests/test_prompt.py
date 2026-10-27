"""The prompt guard: sanitising, folding, dropping and fitting, with explicit limits."""

import unittest

from observer_router.config import Provider
from observer_router.prompt import PromptGuard, PromptLimits, message_chars


def _guard(**limits) -> PromptGuard:
    fields = {"max_prompt_chars": 1_000, "max_field_chars": 400,
              "min_retained_chars": 32, "base64_blob_chars": 16_384}
    fields.update(limits)
    return PromptGuard(PromptLimits(**fields))


class CompactionTests(unittest.TestCase):
    """The guard bounds a conversation while preserving system and newest context."""

    def test_small_messages_are_unchanged(self):
        messages = [
            {"role": "system", "content": "Observe accurately."},
            {"role": "user", "content": "Summarize this session."},
        ]

        compacted, stats = _guard().compact(messages)

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

        compacted, stats = _guard().compact(messages)
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

        compacted, stats = _guard().compact(messages)
        combined = "".join(str(message["content"]) for message in compacted)

        self.assertLessEqual(message_chars(compacted), 1_000)
        self.assertEqual(compacted[0]["role"], "system")
        self.assertIn("newest-", combined)
        self.assertNotIn("old-", combined)
        self.assertTrue(stats["changed"])
        self.assertGreater(stats["dropped_messages"], 0)

    def test_an_oversized_system_message_does_not_starve_the_newest_request(self):
        # Rule: the newest non-system message is the request the model must answer; when the
        # system message alone would take the whole budget, the request keeps up to half of it.
        messages = [{"role": "system", "content": "S" * 5_000},
                    {"role": "user", "content": "Answer this: " + "q" * 600}]

        compacted, _ = _guard(max_field_chars=100_000).compact(messages, 2_000)

        self.assertEqual([m["role"] for m in compacted], ["system", "user"])
        self.assertEqual(compacted[1]["content"], messages[1]["content"])  # small enough: whole
        self.assertLessEqual(message_chars(compacted), 2_000)

    def test_a_request_larger_than_half_the_budget_is_folded_to_half(self):
        # Rule: the share held for the newest message is at most half, so the system contract
        # keeps the rest.
        messages = [{"role": "system", "content": "S" * 5_000},
                    {"role": "user", "content": "Answer this: " + "q" * 5_000}]

        compacted, _ = _guard(max_field_chars=100_000).compact(messages, 4_000)

        sizes = [len(m["content"]) for m in compacted]
        self.assertEqual(len(compacted), 2)
        self.assertLessEqual(sum(sizes), 4_000)
        self.assertGreaterEqual(min(sizes), 1_800)  # neither side is starved
        self.assertTrue(compacted[1]["content"].startswith("Answer this:"))

    def test_compacting_the_same_input_twice_gives_the_same_result(self):
        # Rule: it never calls another model, so the answer cannot vary during an outage.
        messages = [{"role": "system", "content": "S" * 800},
                    {"role": "user", "content": "request " + "r" * 400}]

        first, _ = _guard().compact(messages)
        second, _ = _guard().compact(messages)

        self.assertEqual(first, second)

    def test_a_retained_minimum_is_respected(self):
        # Rule: a message is kept whole or folded to at least the retained minimum; a budget
        # smaller than that drops it instead.
        messages = [{"role": "user", "content": "x" * 900}]

        compacted, stats = _guard(min_retained_chars=200).compact(messages, 100)

        self.assertEqual(compacted, [])
        self.assertEqual(stats["dropped_messages"], 1)


class FitTests(unittest.TestCase):
    """fit sizes a request for one provider; its limit can only tighten the router-wide guard."""

    def provider(self, **changes) -> Provider:
        fields = {"name": "p", "type": "openai", "url": "http://h/v1", "model": "m",
                  "budget": 40.0}
        fields.update(changes)
        return Provider(**fields)

    def test_a_provider_without_a_limit_gets_the_body_as_is(self):
        body = {"messages": [{"role": "user", "content": "x" * 2_000}]}

        self.assertEqual(_guard().fit(self.provider(), body), body)

    def test_a_provider_limit_wins_over_the_default(self):
        body = {"messages": [{"role": "user", "content": "x" * 2_000}]}

        fitted = _guard().fit(self.provider(max_prompt_chars=500), body)

        self.assertLessEqual(message_chars(fitted["messages"]), 500)

    def test_fitting_logs_what_it_changed(self):
        lines = []
        guard = PromptGuard(PromptLimits(max_prompt_chars=1_000, max_field_chars=400,
                                         min_retained_chars=32, base64_blob_chars=16_384),
                            log=lines.append)
        body = {"messages": [{"role": "user", "content": "x" * 2_000}]}

        guard.fit(self.provider(max_prompt_chars=500), body)

        self.assertTrue(lines and "compacted prompt for tier p" in lines[0], lines)


class SnapshotTests(unittest.TestCase):
    """The snapshot reports the limits and the counters since start-up."""

    def test_counters_accumulate_only_for_changed_requests(self):
        guard = _guard()

        guard.record({"changed": False, "input_chars": 5, "output_chars": 5,
                      "dropped_messages": 0, "removed_image_parts": 0, "removed_blobs": 0})
        guard.record({"changed": True, "input_chars": 900, "output_chars": 400,
                      "dropped_messages": 2, "removed_image_parts": 1, "removed_blobs": 3})

        snapshot = guard.snapshot()

        self.assertEqual(snapshot["max_prompt_chars"], 1_000)
        self.assertEqual(snapshot["max_field_chars"], 400)
        self.assertEqual(snapshot["compacted_requests"], 1)
        self.assertEqual(snapshot["input_chars"], 900)
        self.assertEqual(snapshot["output_chars"], 400)
        self.assertEqual(snapshot["dropped_messages"], 2)
        self.assertEqual(snapshot["removed_image_parts"], 1)
        self.assertEqual(snapshot["removed_blobs"], 3)


if __name__ == "__main__":
    unittest.main()
