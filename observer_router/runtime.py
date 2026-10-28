"""The router's state and its chain policy.

One `Runtime` holds everything that changes while the router serves: the active configuration,
the request rotation, the breakers, the gates and the prompt guard. Start-up builds it and the
server carries it, so a test builds its own and two runtimes share nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from observer_router.breakers import Breakers
from observer_router.config import Config, Provider
from observer_router.failure import TierFailure
from observer_router.idle import IdleMonitor, LaneLocks, read_lane
from observer_router.log import log as default_log
from observer_router.power import PowerGate, probe_power
from observer_router.prompt import PromptGuard, PromptLimits
from observer_router.repeats import Repeats
from observer_router.settings import Settings

Log = Callable[[str], None]

# Provider fields a config can override; /health shows the ones that are set.
OVERRIDE_FIELDS = ("max_prompt_chars", "break_after", "break_seconds")


@dataclass
class Runtime:
    """The active chain: who may answer, what is blocked, and the state each gate keeps."""

    settings: Settings
    config: Config
    chain: tuple[str, ...]
    groups: dict[str, tuple[str, ...]]
    breakers: Breakers
    power: PowerGate
    idle: IdleMonitor
    guard: PromptGuard
    lanes: LaneLocks
    repeats: Repeats
    rotation: dict[str, int] = field(default_factory=dict)
    _rotation_lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def build(cls, settings: Settings, config: Config, chain: Iterable[str] | None = None,
              *, probe: Callable[[], str] = probe_power,
              fetch: Callable[[str, dict | None], tuple[int | None, int] | None] = read_lane,
              log: Log = default_log) -> Runtime:
        """Assemble a runtime from a parsed config and the process settings."""
        groups = dict(config.groups)
        providers = dict(config.providers)
        return cls(
            settings=settings,
            config=config,
            chain=tuple(chain or config.chain),
            groups=groups,
            breakers=Breakers(groups, providers, settings.break_after, settings.break_seconds,
                              settings.group_strikes, log),
            power=PowerGate(probe, settings.power_cache_seconds, log),
            idle=IdleMonitor(fetch, settings.idle_sample_seconds),
            guard=PromptGuard(PromptLimits(settings.max_prompt_chars, settings.max_field_chars,
                                           settings.min_retained_chars,
                                           settings.base64_blob_chars), log),
            lanes=LaneLocks(),
            repeats=Repeats(settings.repeat_window_seconds),
        )

    @property
    def providers(self) -> Mapping[str, Provider]:
        return self.config.providers

    def chain_providers(self) -> list[str]:
        """Every provider the chain can reach, in configured order, groups flattened."""
        return [member for name in self.chain for member in self.groups.get(name, (name,))]

    def expand_chain(self) -> list[str]:
        """Return this request's provider order.

        Each group starts one member further on than it did for the previous request, so load
        rotates across its keys; the remaining members follow in order, so a failing or
        quota-blocked member still falls over inside the group before the chain moves on.
        """
        order = []
        with self._rotation_lock:
            for name in self.chain:
                members = self.groups.get(name)
                if members is None:
                    order.append(name)
                    continue
                start = self.rotation.get(name, 0) % len(members)
                self.rotation[name] = start + 1
                order += members[start:] + members[:start]
        return order

    def chain_entry(self, name: str) -> str:
        """The chain entry a provider is reached through: its group, or itself."""
        return self.breakers.group_of(name) or name

    def request_order(self, key: str) -> tuple[list[str], list[str]]:
        """This request's provider order, and the chain entries that already answered it.

        A client repeating a request it got an answer to did not accept that answer, so the
        entries that gave one are tried after the rest, the one that answered longest ago first.
        """
        order = self.expand_chain()
        answered = self.repeats.answered(key)
        if not answered:
            return order, []
        rank = {entry: index for index, entry in enumerate(answered)}
        fresh = [name for name in order if self.chain_entry(name) not in rank]
        tried = sorted((name for name in order if self.chain_entry(name) in rank),
                       key=lambda name: rank[self.chain_entry(name)])
        return fresh + tried, answered

    def answered(self, key: str, name: str) -> None:
        """Remember that this provider's chain entry answered the request with this key."""
        self.repeats.record(key, self.chain_entry(name))

    def busy_lanes(self, provider: Provider, now: float | None = None) -> list[str]:
        return self.idle.busy(provider, now)

    def busy_reason(self, provider: Provider) -> str | None:
        return self.idle.reason(provider)

    def allowed_tiers(self, order: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
        """Split a request's provider order into the ones worth trying and the ones skipped.

        Skipped providers are reported in the failed-chain response so claude-mem's cooldown
        log explains why a local lane never ran.
        """
        order = self.expand_chain() if order is None else order
        allowed: list[str] = []
        skipped: dict[str, str] = {}
        on_battery = (any(self.providers[name].ac_only for name in order)
                      and self.power.source() == "Battery Power")
        for name in order:
            provider = self.providers[name]
            if provider.ac_only and on_battery:
                skipped[name] = "skipped: on battery (AC-only)"
                continue
            busy = self.busy_reason(provider)
            if busy:
                skipped[name] = busy
                continue
            allowed.append(name)
        return allowed, skipped

    def skip_reason(self, name: str) -> str | None:
        return self.breakers.skip_reason(name)

    def available(self, name: str) -> bool:
        return self.breakers.available(name)

    def ok(self, name: str) -> None:
        self.breakers.ok(name)

    def failed(self, name: str, reason: str) -> None:
        self.breakers.failed(name, reason)

    def quota_exhausted(self, name: str, until: float, quota: str) -> None:
        self.breakers.quota_exhausted(name, until, quota)

    def record_failure(self, tier: str, failure: TierFailure) -> None:
        """Record a failed attempt: a quota block, a breaker failure, or a group strike."""
        if failure.partial or failure.skipped:
            return  # a cut-off depends on the prompt, a skip on other requests: not on health
        if failure.retry_at:
            self.quota_exhausted(tier, failure.retry_at, failure.quota)
            return
        self.failed(tier, str(failure))
        group = self.breakers.group_of(tier)
        if group is None or not failure.overloaded:
            return
        # One timeout is ordinary tail latency (a budget sits near the p99), and one
        # unexplained 429 can be one key; two members failing so in a row, with no success
        # between, is the model.
        if (failure.overloaded in ("timeout", "exhausted")
                and not self.breakers.strike(group)):
            return
        self.breakers.group_overloaded(group, str(failure))

    def watched_lanes(self) -> tuple[str, ...]:
        """Every /metrics URL an active provider in the chain waits on."""
        return tuple(dict.fromkeys(
            url for name in self.chain_providers() for url in self.providers[name].idle_metrics
        ))

    def lane_signal(self, url: str) -> dict | None:
        """The idle_signal of the first provider in the chain that watches this URL (config
        loading rejects two providers reading one URL differently); None: the Splash counters."""
        return next((self.providers[name].idle_signal for name in self.chain_providers()
                     if url in self.providers[name].idle_metrics), None)

    def sample_lanes_once(self) -> None:
        self.idle.sample_once((url, self.lane_signal(url)) for url in self.watched_lanes())

    def start_sampler(self) -> threading.Thread:
        return self.idle.start(
            lambda: ((url, self.lane_signal(url)) for url in self.watched_lanes()))

    def compact(self, messages: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
        return self.guard.compact(messages, limit)

    def fit(self, provider: Provider, body: dict) -> dict:
        return self.guard.fit(provider, body)

    def record_compaction(self, stats: dict) -> None:
        self.guard.record(stats)

    def health(self) -> dict:
        """The /health payload: the chain, its configuration and every gate's view."""
        providers = self.chain_providers()
        return {
            "status": "ok",
            "chain": list(self.chain),
            "groups": {name: list(self.groups[name]) for name in self.chain
                       if name in self.groups},
            "providers": {
                name: {"type": self.providers[name].type,
                       "model": self.providers[name].model,
                       "budget_s": self.providers[name].budget,
                       **{key: getattr(self.providers[name], key) for key in OVERRIDE_FIELDS
                          if getattr(self.providers[name], key)}}
                for name in providers
            },
            "breaker": self.breakers.snapshot(providers),
            "prompt_guard": self.guard.snapshot(),
            "power": self.power.snapshot([name for name in providers
                                          if self.providers[name].ac_only]),
            "idle_gate": self.idle.snapshot(self.providers, providers),
        }
