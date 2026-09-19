# Does the router leak memory under oversized payloads?

Date: 2026-09-20

## Why this was checked

The router is a long-lived launchd service that parses untrusted, occasionally enormous
payloads. If it leaked per request, it would eventually become the outage instead of
preventing one.

## Method

1. Sample RSS across uptime, idle and under load.
2. Send 9 oversized requests totalling 32.5 MB of parsed input and watch the trend.
3. Exercise the streaming path separately.
4. Count threads after sustained traffic.
5. Read the module for unbounded state.

## Results

| Check | Result |
| --- | --- |
| RSS, idle → peak → idle | 28 MB → 45 MB (under 3.5M-char payloads) → 29 MB |
| 9 oversized requests (32.5 MB parsed) | oscillated 35–45 MB, **no upward trend** |
| Streaming path | RSS 28 → 28 MB; SSE intact (`STREAM_OK`, `[DONE]`) |
| Threads | 6–9 tracking live connections, single digits after 1600+ served requests |
| Failover | 607 tier failures across 1,619 served requests, all recovered |
| claude-mem | `consecutiveFailures: 0`, last success seconds old |

## Static analysis

Module state is fixed-size only:

- `_failures` and `_skip_until`, both keyed by the configured chain and bounded by tier count.
- `_settings_cache`, refreshed on file mtime change and replaced, not appended.

No unbounded container grows per request, and no exception object is retained.

## Conclusion

No leak. Peak RSS is an allocator high-water mark bounded by the largest single request, and
it returns to baseline when that request is done. This is the expected shape for this workload
and needs no mitigation.
