# GPU-lane gates

Two per-instance gates keep a local GPU lane from competing with the machine's own work. Both
are `openai`-type fields and both fail safe: a probe that cannot be read leaves the instance
available.

## Power rule — GPU lanes run only on AC

A local lane is a GPU workload. With the MacBook unplugged, starting it is a poor trade, so an
`openai` instance with `ac_only = true` is skipped and the failed-chain body names the reason:

```text
{"error": "all tiers failed", "attempted": ["local(skipped: on battery (AC-only))"]}
```

- The instance is skipped whenever `pmset -g batt` reports `Battery Power`. Desktops always
  report AC, so the gate is inert there. Instances without `ac_only` run on battery.
- The probe is cached for `OBSERVER_POWER_CACHE_SECONDS` (30 s) and never runs per request.
- If `pmset` is missing, hung, or unreadable, the source reads `unknown` and the instance
  **stays available** — a failed probe must not take the observer offline.
- `/health` exposes `power.source`, `power.ac_only` (the gated instances), and
  `power.ac_only_allowed`.
- `ac_only` reads the battery of the machine **running the router**. On a provider whose `url`
  names another host it cannot see that host's power, and start-up logs a warning for it; leave
  `ac_only` off for a lane on another machine.

Skipping an instance is safe for claude-mem, which retries a failed chain after a cooldown; see
[Use with claude-mem](../guides/claude-mem.md#when-every-provider-fails).

## Idle rule — a GPU lane waits for interactive lanes

A local lane shares the GPU with any interactive lanes on the same machine. Give its instance
`idle_metrics`, a list of those lanes' Prometheus `/metrics` URLs, and it runs only once every
one of them has been idle for `idle_seconds` (30 s). Without `idle_metrics` the gate is off.

- A lane is active while `submitted − completed − cancelled − failed` is above zero, or when
  its submitted counter moved since the previous sample. The second rule catches a request that
  started and finished between samples. By default the counters read are Splash's
  `splash_requests_{submitted,completed,cancelled,failed}_total`; see
  [naming the metrics](#naming-the-metrics) for any other server.
- A background thread samples every `OBSERVER_IDLE_SAMPLE_SECONDS` (2 s), covering the lanes of
  every instance in the active chain. The grace period covers the pause an agent loop takes
  between requests while its tools run.
- The gate is read when the request arrives and again just before the instance runs, because
  the instances ahead of it can take tens of seconds.
- An unreachable or unreadable lane is not activity: it ages into idle, so a stopped lane cannot
  keep the observer offline.
- A skipped instance is named in the failed-chain body, for example
  `local(skipped: lanes busy (127.0.0.1:1240, idle 30s required))`, and claude-mem retries
  after its cooldown, as with the power rule.
- A call already running is not interrupted when a lane becomes active.
- `/health` reports `idle_gate.<name>.seconds_since_active` per lane and
  `idle_gate.<name>.allowed`.

### Naming the metrics

`idle_signal` in the provider's table says which `/metrics` names mean the lane is busy, for a
server that is not Splash. It needs `idle_metrics`, and takes one of two shapes:

```toml
# counters: busy while submitted - sum(finished) > 0, or when submitted moved between samples
idle_signal = { submitted = "lane_requests_started_total", finished = ["lane_requests_done_total"] }

# gauges: busy while the sum of these is above zero; a gauge with several label sets (one per
# model) adds them up. vLLM, for example:
idle_signal = { in_flight = ["vllm:num_requests_running", "vllm:num_requests_waiting"] }
```

A gauge has no submitted count, so a request that starts and finishes between two samples goes
unseen; the grace period (`idle_seconds`) covers the pause between requests. A named metric that
is missing makes the lane unreadable, which ages into idle as above. One URL is sampled once, so
two providers that watch it must name the same `idle_signal`, whether or not both are in the
chain; the config is rejected otherwise.
