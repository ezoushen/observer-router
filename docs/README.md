# Documentation — observer router

Start with the [README](../README.md) to get a router running. Everything else is here.

## Use it

| Guide | When to read it |
| --- | --- |
| [Build your chain](guides/build-a-chain.md) | Choosing providers, their order, key pools, and per-provider behaviour |
| [Use with claude-mem](guides/claude-mem.md) | Pointing claude-mem at the router, and its timeouts |
| [Install as a macOS service](guides/install.md) | Running the router under launchd |
| [Operate the router](guides/operations.md) | Health, logs, testing one provider alone, and the test suite |

## How it behaves

| Reference | What it covers |
| --- | --- |
| [Configuration](reference/configuration.md) | Every config-file field and environment setting, with defaults and accepted values |
| [Routing and streaming](reference/routing.md) | What counts as an answer or a failed attempt, cut-off replies, the streaming contract, buffered streams |
| [Breakers, quota blocks, and group blocks](reference/breakers-and-quotas.md) | When a provider or a whole group is skipped, and for how long |
| [Prompt guard](reference/prompt-guard.md) | How every request is bounded before any provider sees it |
| [GPU-lane gates](reference/gates.md) | The AC-power and idle rules for local lanes |
| [Known limitations](reference/limitations.md) | What the router does not handle yet |

## Why it is this way

| Folder | Contents |
| --- | --- |
| [`researches/`](researches/README.md) | Investigations and measurements behind the rules, including dead ends |
| [`plans/`](plans/README.md) | Durable plans awaiting an owner decision or execution |
| [`journals/`](journals/README.md) | Dated work log, append-only |

The guides and reference describe the router as it is **now**. Measurements and history live in
the last three folders, and notable changes in [`CHANGELOG.md`](../CHANGELOG.md).

## Scope

This repository owns the router, its launchd plist, the example config, its tests, and this
documentation. The providers behind it, including any local lane and its launchd job, are
separate projects.
