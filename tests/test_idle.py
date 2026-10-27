"""Lane activity: metric parsing, sampling, the idle decision and the serial lane lock."""

import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from observer_router.config import Provider
from observer_router.idle import IdleMonitor, LaneLocks, parse_lane_metrics, read_lane


URL = "http://127.0.0.1:1240/metrics"

SPLASH_METRICS = """# TYPE splash_requests_submitted_total counter
splash_requests_submitted_total {submitted}
# TYPE splash_requests_completed_total counter
splash_requests_completed_total {completed}
splash_requests_cancelled_total 10
splash_requests_failed_total 0
"""

VLLM_METRICS = """# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{{model_name="m"}} {running}
vllm:num_requests_waiting{{model_name="m"}} {waiting}
vllm:cache_usage_perc{{model_name="m"}} 0.4
"""

VLLM_SIGNAL = {"in_flight": ["vllm:num_requests_running", "vllm:num_requests_waiting"]}
CUSTOM_COUNTERS = {"submitted": "lane_started_total",
                   "finished": ["lane_done_total", "lane_err_total"]}


def _provider(**changes) -> Provider:
    fields = {"name": "local", "type": "openai", "url": "http://127.0.0.1:1240/v1",
              "model": "m", "budget": 40.0, "serial": True, "idle_metrics": (URL,),
              "idle_seconds": 30.0}
    fields.update(changes)
    return Provider(**fields)


class MetricParsingTests(unittest.TestCase):
    """parse_lane_metrics reads a Prometheus body into (submitted, in_flight) or None."""

    def test_in_flight_is_submitted_minus_finished(self):
        self.assertEqual(parse_lane_metrics(SPLASH_METRICS.format(submitted=1011, completed=1000)),
                         (1011, 1))

    def test_whitespace_the_server_adds_does_not_make_a_lane_unreadable(self):
        # Rule: trailing and doubled spaces around a value must not stop the gate gating.
        text = ("splash_requests_submitted_total 1011 \n"
                "splash_requests_completed_total  1000\n"
                "splash_requests_cancelled_total 10\t\n"
                "splash_requests_failed_total 0\n")

        self.assertEqual(parse_lane_metrics(text), (1011, 1))

    def test_a_missing_counter_is_unreadable(self):
        self.assertIsNone(parse_lane_metrics("splash_requests_submitted_total 3\n"))

    def test_a_value_that_is_not_finite_makes_the_lane_unreadable(self):
        # Rule: NaN, infinity or an absurd number is an unreadable lane, never an exception.
        for value in ("NaN", "+Inf", "1e400"):
            with self.subTest(value):
                text = SPLASH_METRICS.format(submitted=value, completed=1000)
                self.assertIsNone(parse_lane_metrics(text))
                gauge = f'vllm:num_requests_running {value}\nvllm:num_requests_waiting 0\n'
                self.assertIsNone(parse_lane_metrics(gauge, VLLM_SIGNAL))

    def test_a_missing_named_gauge_is_unreadable(self):
        self.assertIsNone(parse_lane_metrics("vllm:num_requests_running 0\n", VLLM_SIGNAL))

    def test_counters_can_be_named(self):
        text = "lane_started_total 50\nlane_done_total 40\nlane_err_total 3\n"

        self.assertEqual(parse_lane_metrics(text, CUSTOM_COUNTERS), (50, 7))

    def test_gauges_with_labels_are_summed_as_in_flight(self):
        text = VLLM_METRICS.format(running="2.0", waiting="1.0")

        self.assertEqual(parse_lane_metrics(text, VLLM_SIGNAL), (None, 3))

    def test_a_gauge_with_several_label_sets_adds_them_up(self):
        # Rule: a server that reports one gauge per model is busy when any model is.
        text = ('vllm:num_requests_running{model_name="a"} 1.0\n'
                'vllm:num_requests_running{model_name="b"} 2.0\n')

        self.assertEqual(parse_lane_metrics(text, {"in_flight": ["vllm:num_requests_running"]}),
                         (None, 3))


class IdleMonitorTests(unittest.TestCase):
    """The monitor keeps each lane's last activity and answers from it."""

    def monitor(self) -> IdleMonitor:
        return IdleMonitor(fetch=lambda url, signal: None, sample_seconds=2.0)

    def test_a_lane_never_seen_active_is_idle(self):
        monitor = self.monitor()

        self.assertEqual(monitor.busy(_provider(), now=1_000.0), [])

    def test_an_in_flight_request_makes_the_lane_busy_for_the_grace_period(self):
        monitor = self.monitor()
        monitor.record(URL, (1011, 1), now=1_000.0)

        self.assertEqual(monitor.busy(_provider(), now=1_029.0), [URL])
        self.assertEqual(monitor.busy(_provider(), now=1_030.0), [])

    def test_a_request_finished_between_samples_still_counts_as_activity(self):
        monitor = self.monitor()
        monitor.record(URL, (1010, 0), now=1_000.0)
        monitor.record(URL, (1012, 0), now=1_002.0)

        self.assertEqual(monitor.busy(_provider(), now=1_020.0), [URL])

    def test_an_unreadable_lane_ages_into_idle(self):
        monitor = self.monitor()
        monitor.record(URL, (1011, 1), now=1_000.0)
        monitor.record(URL, None, now=1_010.0)

        self.assertEqual(monitor.busy(_provider(), now=1_031.0), [])

    def test_a_gauge_lane_is_busy_only_while_something_is_in_flight(self):
        # Rule: with no submitted counter, in-flight work is the only activity; a lane at zero
        # is idle once the grace period has passed.
        monitor = self.monitor()
        monitor.record(URL, (None, 1), now=1_000.0)
        self.assertEqual(monitor.busy(_provider(), now=1_029.0), [URL])

        monitor.record(URL, (None, 0), now=1_010.0)
        self.assertEqual(monitor.busy(_provider(), now=1_040.0), [])

    def test_the_reason_names_the_lane_and_the_grace(self):
        monitor = self.monitor()
        monitor.record(URL, (1011, 1), now=1_000.0)

        self.assertEqual(monitor.reason(_provider(), now=1_001.0),
                         "skipped: lanes busy (127.0.0.1:1240, idle 30s required)")

    def test_a_provider_without_lanes_has_no_reason(self):
        self.assertIsNone(self.monitor().reason(_provider(idle_metrics=()), now=1_000.0))

    def test_the_snapshot_reports_each_gated_provider(self):
        monitor = self.monitor()
        monitor.record(URL, (1011, 1), now=1_000.0)
        providers = {"remote": _provider(name="remote", idle_metrics=(), serial=False),
                     "local": _provider()}

        snapshot = monitor.snapshot(providers, ["remote", "local"], now=1_010.0)

        self.assertEqual(snapshot, {"local": {"idle_seconds_required": 30.0,
                                             "seconds_since_active": {URL: 10.0},
                                             "allowed": False}})

    def test_sampling_reads_each_lane_with_its_signal(self):
        calls = []

        def fetch(url, signal):
            calls.append((url, signal))
            return (1011, 1)

        monitor = IdleMonitor(fetch=fetch, sample_seconds=2.0)
        monitor.sample_once([(URL, None)])

        self.assertEqual(calls, [(URL, None)])
        self.assertEqual(monitor.busy(_provider()), [URL])  # a fresh reading is activity


class ReadLaneTests(unittest.TestCase):
    """The real reader reads plain http only and treats every failure as no activity."""

    def test_a_lane_is_read_with_its_signal(self):
        class Metrics(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                body = VLLM_METRICS.format(running="1.0", waiting="0.0").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Metrics)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/metrics"

        self.assertEqual(read_lane(url, VLLM_SIGNAL), (None, 1))
        self.assertIsNone(read_lane(url, None))  # the Splash names are not on this lane

    def test_a_non_http_url_is_not_read(self):
        self.assertIsNone(read_lane("file:///etc/passwd"))

    def test_a_lane_answering_something_else_is_unreadable(self):
        class Broken(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Broken)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        self.assertIsNone(read_lane(f"http://127.0.0.1:{server.server_port}/metrics"))

    def test_an_unreachable_lane_is_not_activity(self):
        self.assertIsNone(read_lane("http://127.0.0.1:9/metrics"))


class LaneLockTests(unittest.TestCase):
    """A serial provider holds one lock per lane URL; a non-serial provider holds none."""

    def test_only_serial_providers_share_a_lane_lock(self):
        locks = LaneLocks()
        serial = _provider()
        same_lane = _provider(name="second")
        free = _provider(serial=False)

        self.assertIs(locks.guard(serial), locks.guard(same_lane))
        self.assertIsNot(locks.guard(free), locks.guard(serial))


if __name__ == "__main__":
    unittest.main()
