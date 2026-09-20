# Plan — upstream: one declared provider-resolution seam for claude-mem

Date: 2026-09-20
Status: filed 2026-09-20 as a comment on
[#2785](https://github.com/thedotmack/claude-mem/issues/2785#issuecomment-5747347717); awaiting
the maintainer's response
Evidence: [Provider extensibility survey](../researches/2026-09-20-claude-mem-provider-extensibility-survey.md)

## Objective

Get claude-mem to expose **one declared, provider-neutral point** at which the operator's policy
decides which provider, model, base URL, and credential serve a given observer call — so that
quota exhaustion, gateway routing, and multi-account use stop requiring hand-edited settings and
restarts.

### Success criteria

1. Upstream acknowledges the seam as a bounded slice under #2785 (plan-12), **or** implements it.
2. No duplicate filed: the proposal cites #3941, #3942, #3668, #4075 and the closed #680/#3645.
3. The seam is provider-neutral — it names no provider and builds no built-in chain.
4. Our router remains a working reference implementation of the policy *behind* that seam.

### Explicit non-goals

- Not a request for a built-in Gemini→OpenRouter→local chain. #680 was closed for exactly that.
- Not a duplicate of #3941 (key rotation) or #3942 (generic provider). It is the generalization.
- Not a request to move routing logic into claude-mem. The gateway stays outside.

## Decision summary

| Option | Verdict |
| --- | --- |
| File a broad "model selection policy / provider chain" issue | **Rejected** — re-runs #680, which was closed as premature and over-scoped |
| File a narrow, provider-neutral **resolution seam** issue | **Recommended** — the only variant with no existing home |
| Comment on #2785 asking for it as a plan-12 slice | **Recommended fallback** — lower risk, same substance, routed where the maintainer wants capability asks |
| Do nothing, keep using the base-URL seam | **Viable** — costs us nothing today; the docs already sanction it |

## Ready-to-file draft

Suggested title:

> RFC: one declared provider-resolution seam per observer call (provider, model, base URL, credential)

Body:

---

### Summary

claude-mem resolves its observer provider, model, base URL, and credential **once**, from global
settings, and never re-decides on a classified failure. Operators who hit a daily quota wall
today have two choices: hand-edit `settings.json`/`.env` and restart, or put a gateway in front
and accept that the gateway path is second-class. I would like to propose **one declared seam**
where policy lives, instead of several implicit ones.

### What exists today (verified)

| Mechanism | Scope | Limitation |
| --- | --- | --- |
| `ANTHROPIC_BASE_URL` | `claude` provider | Documented; no auto-detection of existing Claude Code gateway vars |
| `CLAUDE_MEM_OPENROUTER_BASE_URL` | OpenRouter client only | Carries OpenRouter/cmem attribution headers and `usage.include` gating to any host (#3942) |
| `CLAUDE_MEM_PROVIDER` + model | Global, resolved once | No re-decision after `rate_limit`, `quota_exhausted`, or `auth_invalid` |
| open PR #3941 | Multi-key pool per provider | Rotates **keys**, not provider/model/base URL |
| open PR #3942 | Generic `openai-compatible` provider | Adds one more provider; still a single resolved tuple |

### Problem

Three operator needs collide on the same missing seam:

1. **Quota exhaustion** — a free-tier key dies mid-day and generation stalls; the workaround is a
   manual edit plus restart. The maintainer asked (#680) for evidence of regular quota exhaustion
   before reconsidering fallback. That evidence now exists: #4068, #4076, #4083, #4109, #4114,
   #4127, #4134, plus gateway incidents in #4075 and #3664.
2. **Gateways** — LiteLLM and 9router are documented/supported paths, but two measured adapter
   defects (#3664 → PR #3668) show the gateway path is not yet first-class.
3. **Multiple accounts** — the accepted answer today is the key pool in #3941; that solves
   credentials only.

### Proposal

One declared resolution seam — `ObserverSpawnSpec` already declares most of this in plan-19
(#3607) — extended so that the **credential, provider, model, and base URL are resolved per
generation attempt**, not once at import.

Concretely: a single documented function or config block that, given a small request context,
returns:

```json
{ "provider": "openai-compatible", "model": "…", "baseUrl": "…", "credential": "…" }
```

Precedence, in order:

1. an explicit per-request decision from the configured policy (if any);
2. the existing global settings;
3. built-in defaults.

Shape options, cheapest first:

- **A. Declarative deployment list.** An ordered list of `(provider, model, baseUrl, credentialRef)`
  deployments with a selection strategy (`priority`, `quota-aware`, `round-robin`). Policy as
  data — this is LiteLLM's model and needs no user code.
- **B. Command hook.** An `apiKeyHelper`-style command (already requested as #2704) that prints a
  credential, generalized to print the whole tuple.
- **C. Module path.** A user-supplied resolver for exotic policies.

Recommendation: **A**, with **B** as the incremental step that retires #2704. **C** is
unnecessary until A is proven insufficient.

### Relationship to open work

- Generalizes **#3941** (key pool becomes the credential dimension of a deployment).
- Complements **#3942** (a generic provider becomes one implementation behind the seam).
- Completes **plan-19 #3607**'s declared `authSource`, promoting it from spawn attribute to policy.
- Makes **#3664** (9router) and **#4075** (compression proxy) first-class instead of special cases.

### Non-goals

- No built-in provider chain, no provider names baked into core.
- No change to existing settings semantics; a deployment list is additive.

### Acceptance criteria

1. A policy can select a deployment per generation attempt.
2. A non-retryable kind (`quota_exhausted`, `auth_invalid`) re-decides the next attempt instead of
   arming a 30-minute provider-wide cooldown.
3. Credentials are referenced, never echoed into logs.
4. A failure still surfaces loudly — policy must not silently degrade to a provider the operator
   did not allow.
5. Existing single-key, single-provider installs behave identically when no policy is configured.

### Reference implementation

A working external implementation of this policy already exists: an OpenAI-compatible router that
fronts the observer, chains Gemini Flash → OpenRouter free → a local MLX lane, bounds every
prompt before any provider sees it, and gates the local tier on AC power. It runs behind
`CLAUDE_MEM_OPENROUTER_BASE_URL` today, which is exactly the second-class path this RFC would make
first-class.

---

## Alternative: comment on #2785

If filing a new issue is judged too likely to be read as re-raising #680, post the same substance
as a comment on **#2785 (plan-12)**, in its **Providers & auth** sub-area, framed as a child:

> Requesting one bounded slice for the Providers & auth sub-area: a declarative deployment list
> (`provider`, `model`, `baseUrl`, credential reference) with per-attempt resolution, so that
> #3941's key pool and #3942's generic provider become two dimensions of one resolution point
> rather than two parallel mechanisms. Evidence of regular quota exhaustion, which #680's review
> asked for: …

The maintainer's own consolidation rounds show that this is how capability asks land: they are
folded into #2785 and closed as children, not merged as standalone PRs.

## Evidence pack

| Claim | Evidence |
| --- | --- |
| Free quotas are exhausted under ordinary observer load | Gemini: 471 calls then `exceeded your current quota`; OpenRouter: `free-models-per-day` |
| Quota failures are frequent upstream, not ours alone | Open issues #4068, #4076, #4083, #4109, #4114, #4127, #4134 |
| Gateways are a sanctioned but second-class path | Docs "Custom Anthropic-Compatible Backends"; #2785 records two measured adapter defects |
| Our router is a working policy implementation | `docs/researches/2026-09-20-observer-router-memory-profile.md`, prompt guard and AC gate in the README |
| The idea is not already filed | Survey keyword search over all issue/PR states returned no resolution-seam thread |

## Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Read as a duplicate of #680 / #3645 | High | Cite both explicitly; lead with what is *not* asked for |
| Routed to #2785 and left open indefinitely | High | Frame as one bounded slice with acceptance criteria, not a master |
| Adjacent PRs (#3941/#3942) supersede the ask | Medium | Position the seam as their generalization, which strengthens the case |
| Linking our private repo publicly | Certain | Repo is **private** today; the reference-implementation paragraph needs a decision first |
| Policy silently degrading reliability | Medium | Acceptance criterion 4 requires loud failure, never silent downgrade |

## Decision points

1. **New issue, comment on #2785, or hold?** — **Resolved:** filed as a comment on #2785, the
   route the maintainer's own consolidation rounds use for capability asks. A new issue is only
   worth opening if the maintainer asks for one.
2. **May the reference implementation be cited publicly?** — **Resolved:** the repository was
   made public and cited in the comment.
3. **Is the owner willing to author a PR** if the maintainer accepts the slice? If yes, scope it to
   shape **A** plus tests.

## Sequencing

1. ~~Owner picks the filing route and citation permission~~ — **done 2026-09-20.**
2. ~~Post the draft~~ — **done:** comment on #2785.
3. Awaiting a response. If accepted: scope shape **A**, open a PR, and use this router as the
   acceptance fixture (decision 3).
4. Independently, keep using the base-URL seam — nothing here blocks current operation.

## Verification if upstream implements it

- Point `CLAUDE_MEM_OPENROUTER_BASE_URL` at a policy-free install and confirm identical behaviour.
- Configure a two-deployment policy (remote + local) and kill the remote tier; confirm the next
  attempt re-decides rather than arming a provider-wide cooldown.
- Confirm credentials never appear in worker logs.
- Confirm a misconfigured policy fails loudly instead of silently downgrading.
