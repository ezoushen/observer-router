"""Repeated requests: which chain entries already answered an identical request."""

import unittest

from observer_router.repeats import Repeats, request_key


class RequestKeyTests(unittest.TestCase):

    def test_the_key_ignores_field_order(self):
        # Rule: two bodies that parse to the same JSON are the same request.
        first = request_key({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        second = request_key({"messages": [{"content": "hi", "role": "user"}], "model": "m"})

        self.assertEqual(first, second)

    def test_any_difference_makes_another_request(self):
        base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}

        self.assertNotEqual(request_key(base), request_key({**base, "temperature": 0.2}))
        self.assertNotEqual(request_key(base),
                            request_key({**base, "messages": [{"role": "user", "content": "ho"}]}))


class RepeatsTests(unittest.TestCase):

    def test_a_new_request_has_no_answers(self):
        self.assertEqual(Repeats(600).answered("k", now=0.0), [])

    def test_entries_that_answered_are_listed_least_recent_first(self):
        # Rule: the entry that answered longest ago is the first to be tried again.
        repeats = Repeats(600)
        repeats.record("k", "local", now=0.0)
        repeats.record("k", "cursor", now=60.0)
        repeats.record("k", "local", now=120.0)

        self.assertEqual(repeats.answered("k", now=130.0), ["cursor", "local"])

    def test_an_answer_older_than_the_window_is_forgotten(self):
        repeats = Repeats(600)
        repeats.record("k", "local", now=0.0)
        repeats.record("k", "cursor", now=500.0)

        self.assertEqual(repeats.answered("k", now=650.0), ["cursor"])
        self.assertEqual(repeats.answered("k", now=1200.0), [])

    def test_answers_are_kept_per_request(self):
        repeats = Repeats(600)
        repeats.record("k", "local", now=0.0)

        self.assertEqual(repeats.answered("other", now=1.0), [])

    def test_a_zero_window_remembers_nothing(self):
        repeats = Repeats(0)
        repeats.record("k", "local", now=0.0)

        self.assertEqual(repeats.answered("k", now=0.0), [])

    def test_memory_is_bounded(self):
        # Rule: past the limit the request answered longest ago is dropped first.
        repeats = Repeats(600, limit=2)
        repeats.record("a", "local", now=0.0)
        repeats.record("b", "local", now=1.0)
        repeats.record("c", "local", now=2.0)

        self.assertEqual(repeats.answered("a", now=3.0), [])
        self.assertEqual(repeats.answered("b", now=3.0), ["local"])
        self.assertEqual(repeats.answered("c", now=3.0), ["local"])


if __name__ == "__main__":
    unittest.main()
