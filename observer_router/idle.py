"""Lane activity: the idle gate and the serial lane lock.

A local provider shares the GPU with interactive lanes on the same machine. With `idle_metrics`
naming those lanes' Prometheus `/metrics` URLs, the provider runs only after every one of them
has been idle for its `idle_seconds`, so an observer prefill never competes with a turn the user
is waiting on. The grace period matters because an agent loop pauses between requests while its
tools run.

A sampler, not a per-request probe, tracks activity: a request that starts and finishes between
two observer calls still moves the submitted counter, but only a sampler can say when. An
unreadable lane keeps its last reading and ages into idle, so a stopped or broken lane cannot
keep the observer offline.
"""

from __future__ import annotations

import http.client
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping

from observer_router.config import Provider

DEFAULT_SAMPLE_SECONDS = 2.0

# What counts as busy when a provider sets no idle_signal: Splash's request counters.
SPLASH_SIGNAL = {
    "submitted": "splash_requests_submitted_total",
    "finished": ["splash_requests_completed_total", "splash_requests_cancelled_total",
                 "splash_requests_failed_total"],
}


def parse_lane_metrics(text: str, signal: dict | None = None) -> tuple[int | None, int] | None:
    """Return (submitted, in_flight) from a Prometheus /metrics body, or None when a named metric
    is missing or unreadable.

    The signal is an idle_signal table: counters, {submitted, finished}, give in-flight as
    submitted minus the finished counters; gauges, {in_flight}, are summed (a labelled gauge
    adds up over its label sets) and have no submitted count, so None stands for it.
    """
    signal = signal or SPLASH_SIGNAL
    wanted = set(signal["in_flight"] if "in_flight" in signal
                 else [signal["submitted"], *signal["finished"]])
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rstrip().rpartition(" ")
        name = head.rstrip().partition("{")[0]
        if name in wanted:
            try:
                values[name] = values.get(name, 0.0) + float(value)
            except ValueError:
                return None
    if wanted - values.keys():
        return None
    try:
        if "in_flight" in signal:
            return None, int(sum(values.values()))
        submitted = int(values[signal["submitted"]])
        return submitted, submitted - int(sum(values[name] for name in signal["finished"]))
    except (ValueError, OverflowError):  # NaN, infinity: not a reading
        return None


def read_lane(url: str, signal: dict | None = None) -> tuple[int | None, int] | None:
    """Read one lane's /metrics over plain http; an unreachable lane is not activity."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return None
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=1.0)
    try:
        connection.request("GET", parts.path or "/metrics")
        response = connection.getresponse()
        if response.status != 200:
            return None
        return parse_lane_metrics(response.read().decode("utf-8", "ignore"), signal)
    except Exception:  # an unreachable lane is not activity
        return None
    finally:
        connection.close()


class _NullGuard:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class LaneLocks:
    """One lock per lane URL, held only by providers marked serial.

    ThreadingHTTPServer will happily run two upstream calls at once, and two concurrent prefills
    on the same lane both slow down and inflate its KV pool; a serial provider takes the lock of
    its URL, so two instances pointed at one lane share it.
    """

    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def guard(self, provider: Provider):
        """Hold the provider's lane for the duration of a call when it is serial."""
        if not provider.serial:
            return _NullGuard()
        with self._lock:
            return self._locks.setdefault(provider.url, threading.Lock())


class IdleMonitor:
    """Each watched lane's last activity, sampled through `fetch` and answered on demand.

    `fetch(url, signal)` returns (submitted, in_flight) or None; `sample_seconds` is the sampler
    interval. The monitor holds one reading per lane URL, whatever provider watches it.
    """

    def __init__(self, fetch: Callable[[str, dict | None], tuple[int | None, int] | None] = read_lane,
                 sample_seconds: float = DEFAULT_SAMPLE_SECONDS):
        self._fetch = fetch
        self.sample_seconds = sample_seconds
        self._lock = threading.Lock()
        self._activity: dict[str, dict] = {}

    def record(self, url: str, sample: tuple[int | None, int] | None, now: float) -> None:
        """Fold one reading into the lane's activity: any new or in-flight request is activity."""
        if sample is None:
            return
        submitted, in_flight = sample
        with self._lock:
            state = self._activity.setdefault(url, {"submitted": submitted, "busy_at": 0.0})
            if in_flight > 0 or submitted != state["submitted"]:
                state["busy_at"] = now
            state["submitted"] = submitted

    def sample_once(self, lanes: Iterable[tuple[str, dict | None]]) -> None:
        """Read every lane once; a lane whose signal changes is read only once per URL."""
        for url, signal in lanes:
            self.record(url, self._fetch(url, signal), time.time())

    def busy(self, provider: Provider, now: float | None = None) -> list[str]:
        """Return the provider's watched lanes active within its last idle_seconds."""
        now = time.time() if now is None else now
        with self._lock:
            return [
                url for url in provider.idle_metrics
                if now - self._activity.get(url, {}).get("busy_at", 0.0) < provider.idle_seconds
            ]

    def reason(self, provider: Provider, now: float | None = None) -> str | None:
        """Why the provider must wait, or None when every lane it watches is idle enough."""
        busy = self.busy(provider, now)
        if not busy:
            return None
        ports = ",".join(urllib.parse.urlsplit(url).netloc for url in busy)
        return f"skipped: lanes busy ({ports}, idle {provider.idle_seconds:.0f}s required)"

    def snapshot(self, providers: Mapping[str, Provider], order: Iterable[str],
                 now: float | None = None) -> dict:
        """Describe the gate for `/health`, one entry per gated provider in the chain order."""
        now = time.time() if now is None else now
        snapshot = {}
        for name in order:
            provider = providers[name]
            if not provider.idle_metrics:
                continue
            with self._lock:
                lanes = {
                    url: round(now - self._activity[url]["busy_at"], 1)
                    if url in self._activity and self._activity[url]["busy_at"] else None
                    for url in provider.idle_metrics
                }
            snapshot[name] = {
                "idle_seconds_required": provider.idle_seconds,
                "seconds_since_active": lanes,
                "allowed": not self.busy(provider, now),
            }
        return snapshot

    def start(self, lanes: Callable[[], Iterable[tuple[str, dict | None]]]) -> threading.Thread:
        """Sample `lanes()` in a daemon thread so activity is seen between requests."""
        def loop():
            while True:
                self.sample_once(lanes())
                time.sleep(self.sample_seconds)

        thread = threading.Thread(target=loop, name="idle-sampler", daemon=True)
        thread.start()
        return thread
