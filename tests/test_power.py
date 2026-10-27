"""The AC-power probe and the ac_only decision: cached, injected and fail-safe."""

import unittest

from observer_router.power import PowerGate


class PowerGateTests(unittest.TestCase):
    """The gate answers from a probe it is given, never shelling out during a test."""

    def test_the_source_is_what_the_probe_answered(self):
        gate = PowerGate(lambda: "Battery Power", cache_seconds=30.0)

        self.assertEqual(gate.source(now=1_000.0), "Battery Power")

    def test_an_unreadable_source_is_unknown(self):
        # Rule: a probe that fails must not take the observer offline; it reads as unknown.
        def broken():
            raise OSError("pmset is gone")

        gate = PowerGate(broken, cache_seconds=30.0)

        self.assertEqual(gate.source(now=1_000.0), "unknown")

    def test_a_probe_that_answers_nonsense_is_unknown(self):
        gate = PowerGate(lambda: "mains", cache_seconds=30.0)

        self.assertEqual(gate.source(now=1_000.0), "unknown")

    def test_the_answer_is_cached_for_the_window(self):
        calls = []

        def probe():
            calls.append(1)
            return "AC Power"

        gate = PowerGate(probe, cache_seconds=30.0)
        gate.source(now=1_000.0)
        gate.source(now=1_029.0)

        self.assertEqual(len(calls), 1)
        gate.source(now=1_031.0)
        self.assertEqual(len(calls), 2)

    def test_only_a_change_is_logged(self):
        # Rule: the transition is logged, and the same answer a second time is not.
        answers = iter(["AC Power", "Battery Power", "Battery Power"])
        lines = []
        gate = PowerGate(lambda: next(answers), cache_seconds=0.0, log=lines.append)

        gate.source(now=1_000.0)
        gate.source(now=1_001.0)
        gate.source(now=1_002.0)

        self.assertEqual(lines, ["power source unknown -> AC Power",
                                 "power source AC Power -> Battery Power"])

    def test_the_snapshot_keeps_ac_only_off_on_battery(self):
        gate = PowerGate(lambda: "Battery Power", cache_seconds=30.0)

        snapshot = gate.snapshot(["local"], now=1_000.0)

        self.assertEqual(snapshot, {"source": "Battery Power", "ac_only": ["local"],
                                    "ac_only_allowed": False})

    def test_the_snapshot_allows_ac_only_when_the_source_is_unknown(self):
        gate = PowerGate(lambda: "unknown", cache_seconds=30.0)

        self.assertTrue(gate.snapshot(["local"], now=1_000.0)["ac_only_allowed"])


if __name__ == "__main__":
    unittest.main()
