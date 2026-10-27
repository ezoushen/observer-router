# Build your chain

Everything about the chain lives in one TOML file; the
[configuration reference](../reference/configuration.md) lists every field.

- **Providers.** Each `[providers.<name>]` table is one instance: a type, a model, where its key
  lives, and how long it may take. Define as many as you want, several of one type included —
  three Gemini keys are three instances, each with its own breaker and quota block.
- **Order.** `chain` lists provider names in the order they are tried. Reorder it, drop an
  entry, or add one; nothing else changes. `OBSERVER_ROUTER_CHAIN` overrides it for one run
  without editing the file.
- **Groups.** A chain entry may name a `[groups.<name>]` of providers instead. A group rotates:
  each request starts at the next member and the rest follow, so load spreads across several keys
  and a blocked member still falls over to its siblings before the chain moves on.
- **Per-provider behaviour.** Budgets, request fields (`reasoning_effort`,
  `chat_template_kwargs`), stream buffering, and the GPU-lane gates (`serial`, `ac_only`,
  `idle_metrics`) are set per instance, so one chain can mix fast hosted tiers with a slow local
  lane.
- **Per-provider overrides of that policy.** `max_prompt_chars`, `break_after` and `break_seconds`
  let one provider differ from the router-wide defaults, for a small-context lane or a flaky one.
- **Router-wide policy.** Breaker thresholds, the prompt guard, and sampling intervals are
  environment variables with defaults; see the [configuration reference](../reference/configuration.md#environment).

Three provider types cover the upstreams:

| Type | Upstream | Default budget | What the type adds |
| --- | --- | ---: | --- |
| `gemini` | `generativelanguage.googleapis.com/v1beta/openai` | 18 s | Reads Gemini's quota errors and blocks a key until its quota resets |
| `openrouter` | `openrouter.ai/api/v1` | 24 s | Disables reasoning per request; reads `X-RateLimit-Reset`; attribution headers |
| `openai` | any OpenAI-compatible `url` | 40 s | Local lanes and bridges; key optional; the GPU-lane gates and request fields |

For example, a pool of keys first, then a paid fallback, then a local lane as the backstop:

```toml
chain = ["keys", "paid", "local"]

[groups.keys]
members = ["gemini-a", "gemini-b"]
```

or a local lane first, with hosted capacity only when it is busy or down:

```toml
chain = ["local", "gemini-a", "openrouter"]
```

## Size the budgets

The router waits up to each instance's `budget_s` in turn, so the budgets of the instances that
can fail before the one that answers add up. The [request deadline](../reference/routing.md#the-request-deadline)
caps that sum: set `OBSERVER_REQUEST_DEADLINE_SECONDS` just under the client's own timeout, and an
instance's `min_budget_s` to the least time it can answer in; for claude-mem, see
[Use with claude-mem](claude-mem.md#timeouts). An instance with
`buffer_stream = true` spends its budget on the whole reply, not just the first token
([routing](../reference/routing.md#buffered-streams)).
