# Operate the router

| Thing | Value |
| --- | --- |
| launchd label | `com.ezou.observer-router` |
| plist | `~/Library/LaunchAgents/com.ezou.observer-router.plist` (copy kept here) |
| log | `/tmp/observer-router.log` |
| config | `~/.config/observer-router/config.toml` |
| health | `curl -fsS http://127.0.0.1:1244/health` — chain, groups, instances, breaker and quota blocks, prompt guard, power/AC gate, idle gate |
| models | `curl -fsS http://127.0.0.1:1244/v1/models` |

Exercise one instance in isolation by starting a throwaway router whose chain names only it. Use
a port nothing listens on: ports next to 1244 may belong to other local services, so check with
`lsof -nP -iTCP:<port> -sTCP:LISTEN` first, and stop only the process you started.

```bash
OBSERVER_ROUTER_PORT=12944 OBSERVER_ROUTER_CHAIN=<provider-name> python3 observer-router.py
# then POST to :12944 — the log shows which instance served
```

## Tests

```bash
python3 -m unittest -v test_observer_router.py
```

The suite drives the real HTTP handler against fake upstreams. It covers:

- config loading and validation: named instances, type defaults, groups and their rotation, the
  chain and its override, and rejection of inline keys, unknown fields, undefined names, and
  providers reachable twice; invalid environment settings;
- request building: key sources, attribution headers, reasoning fields, lane locks;
- failover between instances, rotation across a group, and group-wide blocks for overloaded
  models;
- replies cut off at the token limit: failover, buffered streams and their whole-reply budget,
  the last-resort cut-off reply, and the log line;
- prompt compaction, the power and idle gates, and quota-reset parsing against the 429 shapes
  Gemini and OpenRouter actually return.
