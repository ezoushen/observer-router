# Reference

What the router does, rule by rule. For step-by-step setup, see the [guides](../guides/README.md).

| Page | Covers |
| --- | --- |
| [Configuration](configuration.md) | The TOML file: providers, groups, the chain, validation; every environment setting |
| [Routing and streaming](routing.md) | Failed attempts, empty and cut-off replies, repeated requests, the streaming contract, buffered streams |
| [Breakers, quota blocks, and group blocks](breakers-and-quotas.md) | Per-provider breakers, quota resets from 429s, group-wide overload blocks |
| [Prompt guard](prompt-guard.md) | Image and blob removal, field folding, and the conversation budget |
| [GPU-lane gates](gates.md) | `ac_only` and the idle rule |
| [Security model](security.md) | What the router protects, who can reach it, and what it leaves to you |
| [Known limitations](limitations.md) | Open gaps and their workarounds |
