# What provider and routing extensibility does claude-mem actually expose?

Date: 2026-09-20

Scope: upstream `thedotmack/claude-mem`, observed on 2026-09-20. At that moment the repository
had **42 open issues** and **183 open pull requests**.

## Question

Can an operator define *how* the observer's provider, model, and credential are chosen — a
"model selection policy" — and would one such seam subsume the multi-account rotation requests
already in the backlog?

## Method

- Enumerated every open issue and open pull request through the GitHub API.
- Keyword-searched issues and PRs in **any** state for: model selection, model override,
  provider selection, routing, policy, API keys, rotation, account, gateway, proxy, base URL.
- Read the maintainer's roadmap masters (`plans/12`, `plans/18`, `plans/19`) and the published
  docs page for custom Anthropic-compatible backends.
- Surveyed prior art from other gateways and routers.

## Findings

### 1. Two routing seams already exist, and both are documented

Cross-provider routing is **already expected to happen outside claude-mem**:

| Protocol | Setting | Documented as |
| --- | --- | --- |
| Anthropic | `ANTHROPIC_BASE_URL` in `~/.claude-mem/.env` | "Custom Anthropic-Compatible Backends" — explicitly recommends putting LiteLLM in front of OpenAI-only providers |
| OpenAI | `CLAUDE_MEM_OPENROUTER_BASE_URL` + `CLAUDE_MEM_OPENROUTER_MODEL` | The OpenRouter client doubles as a generic OpenAI-compatible client |

`CLAUDE_MEM_MODEL` is passed straight through to the SDK and is never translated, so the model
name must be one the gateway accepts. The docs also state plainly: **"No auto-detection"** —
claude-mem will not read an operator's existing Claude Code gateway variables today.

### 2. The gap is first-class-ness and gateway correctness, not the absence of an override

PR **#3942** (open, stacked on #3941) argues precisely why `CLAUDE_MEM_OPENROUTER_BASE_URL` is a
poor home for a generic gateway. It points at three OpenRouter- and cmem.ai-specific things the
client carries into whatever host the base URL names:

- `HTTP-Referer` / `X-Title` attribution headers, sent to the third-party host.
- `usage: { include: true }`, gated on the URL containing `openrouter.ai`.
- (a third item the PR lists in full)

The maintainer separately recorded **two measured gateway adapter defects** in #2785:

1. **`discovery_tokens` is always 0 through `ANTHROPIC_BASE_URL` gateways.** A gateway that
   synthesizes SSE puts the real `input_tokens` in `message_delta` (`estimated: true`) while
   `message_start` reports 0; the Agent SDK assembles from `message_start`, so claude-mem
   persists 0. Measured impact: 1108/1108 observations carried tokens the day before the switch,
   34 of the 50 most recent were 0 afterwards.
2. **The OpenRouter path cannot talk to gateways that default to SSE.** `queryOpenRouterMultiTurn`
   omits `stream` and raw-`JSON.parse`s the response; gateways that default to streaming return
   `text/event-stream`, and the parse throws. Every batch fails until the provider is switched.

Both defects already have an open fix: PR **#3668** `fix(gateways): recover discovery tokens and
unblock SSE-default OpenRouter`.

### 3. Multi-account rotation is in flight, not missing

PR **#3941** adds `CLAUDE_MEM_GEMINI_API_KEYS` / `CLAUDE_MEM_OPENROUTER_API_KEYS`, a key pool, a
kind-specific cooldown (`rate_limit` / `quota_exhausted` / `auth_invalid`), salted key
fingerprints rather than credentials, and exclusion of personal keys from the cmem.ai gateway.
The maintainer routed it explicitly to **#2785 (plan-12)** and called it the bottom of the
`#3941 → #3942` stack, to land first.

**So "multi-account rotating" is already being solved at the provider level.** A new issue that
claims to solve rotation would duplicate #3941.

### 4. Where such an idea would have to live

- **#2785 / plan-12 "Provider & Extensibility Roadmap"** is the declared home for net-new
  capability ("not defects"), with a **Providers & auth** sub-area. It already contains
  #2704 (an `apiKeyHelper` equivalent for refreshable gateway tokens) and #3664 (first-class
  9router support, folded in).
- **plan-19 (#3607)** declares an `ObserverSpawnSpec` with an explicit
  `authSource ∈ {env, keychain, credentials.json, apiKey, apiKeyHelper}` — the closest thing to
  a resolution seam that exists in design form.
- **plan-18 (#3606)** owns the response pipeline: "classify every output, never confirm a batch
  on failure, bound every history."
- **Closed predecessors:** #680 (multi-model + provider chain) was closed unmerged as premature
  and over-scoped; #3645 (per-session/per-project model routing) was closed as not planned and
  consolidated into plan-20 (#3608); #2196, #2527, #2704, #3652 and #3664 are all closed. The
  #680 review left an explicit door open: model fallback "could be reconsidered with evidence of
  regular quota exhaustion."

### 5. No open issue or PR proposes a resolution seam

Searching every state for "model selection policy", "provider policy", "resolution hook" and
"model router" surfaces no thread proposing a per-request provider/model/credential resolution
point. The nearest open threads are:

| Thread | What it actually asks for |
| --- | --- |
| #4075 | Detect a running compression proxy and route the observer through it — for the `claude` provider, already solvable with `ANTHROPIC_BASE_URL`; the ask is detection plus docs |
| #3941 | Multi-key rotation (provider-level) |
| #3942 | A first-class generic `openai-compatible` provider with presets |
| #4100 | Bound a hung provider call and raise generation concurrency |

**Conclusion: the idea is adjacent to several open threads but not itself filed.**

### 6. The evidence the maintainer asked for now exists

The bar that review set is *evidence of regular quota exhaustion*. That evidence is now abundant,
and includes our own measurements:

- Gemini served **471 calls** in a day before `exceeded your current quota`.
- OpenRouter returned `Rate limit exceeded: free-models-per-day`.
- claude-mem recorded **75** `quota exhausted (status 502)` events in one day while still
  storing observations afterwards.
- Upstream's own open issue cluster: #4068, #4076, #4083, #4109, #4114, #4127, #4075, #4134.

## Prior art outside the repository

| Source | Transferable idea |
| --- | --- |
| LiteLLM | The routing unit is a **deployment** = `(model, api_base, api_key)` — not a provider name. Policy picks a deployment. |
| OpenRouter | Provider routing (`provider.order`, price ceilings) and `models[]` fallbacks — policy expressed as **data**, not code. |
| claude-code-router, CC-Router | Multi-key / multi-OAuth rotation with failure counting and cooldown — the #3941 feature class, widely reimplemented. |
| Cache-aware routing writeups | Round-robin across keys or accounts destroys prompt caching and can cost more than it saves; observer calls are independent, so the risk is lower but real. |
| vLLM Semantic Router ([arXiv 2603.04444](https://arxiv.org/html/2603.04444v1)) | Signal-driven decision routing with composable signal orchestration. |
| Aegis gateway | "Policy before optimization": governance is a hard gate that no failure path may silently downgrade. |
| llm-d inference payload processor | Payload-aware external processing in the data plane — the shape of putting policy in a gateway rather than the client. |

## Answer to the question

A "model selection policy" is a sound instinct, but as originally phrased it is:

1. **partly already shipped** — via the two documented base-URL seams;
2. **partly already in flight** — #3941 (key rotation), #3942 (generic provider), #3668 (gateway
   correctness);
3. **structurally destined for #2785/plan-12** rather than a standalone PR, because that master
   owns net-new provider capability and the maintainer has twice declined a broad provider chain.

The defensible narrow ask is therefore *not* "add a provider chain", but **one declared,
provider-neutral resolution seam** that generalizes what #3941 and #3942 are building
independently. See [`../plans/2026-09-20-claude-mem-provider-resolution-seam.md`](../plans/2026-09-20-claude-mem-provider-resolution-seam.md)
for the complete plan and a ready-to-file draft.
