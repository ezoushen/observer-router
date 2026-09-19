# claude-mem provider fallback discussions on GitHub

Date: 2026-09-15  
Repository: [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem)

## Answer

Yes. The official repository has discussed almost this exact idea, but it was not accepted as a native cross-provider feature.

The closest match is [PR #680](https://github.com/thedotmack/claude-mem/pull/680), which proposed:

- a comma-separated list of OpenRouter models with automatic fallback;
- an `OpenRouter -> Gemini -> Claude SDK` provider chain; and
- fallback on quota exhaustion and provider outages.

The PR was closed without merge. The maintainer said it bundled too many features and called both automatic model fallback and the provider chain premature. The maintainer invited a smaller OpenRouter base-URL change and said model fallback could be reconsidered with evidence of regular quota exhaustion.

Part of that proposal was later implemented: [issue #3829](https://github.com/thedotmack/claude-mem/issues/3829) reported that the OpenRouter model-list setting was comma-joined into one invalid model name, and merged [PR #3971](https://github.com/thedotmack/claude-mem/pull/3971) shipped native OpenRouter `models[]` fallback in `claude-mem@13.24.7`. It only applies when talking to `openrouter.ai`; custom OpenAI-compatible base URLs receive the first model only. This supports multiple OpenRouter variants, not OpenRouter-to-Gemini-to-local routing.

Native provider fallback was also deliberately removed earlier. Merged [PR #706](https://github.com/thedotmack/claude-mem/pull/706) says users selecting Gemini or OpenRouter should get that provider rather than a silent Claude fallback. Later [issue #2087](https://github.com/thedotmack/claude-mem/issues/2087) showed that documentation still promised Gemini-to-Claude fallback for HTTP 429 responses; the issue was closed by merged [PR #2141](https://github.com/thedotmack/claude-mem/pull/2141), which removed the never-wired fallback hook rather than implementing the documented behavior.

## Direct and close matches

| Thread | Status on 2026-09-15 | Relevance |
|---|---|---|
| [PR #680: multi-model configuration with automatic fallback](https://github.com/thedotmack/claude-mem/pull/680) | Closed, unmerged | Exact historical match for OpenRouter model variants and an `OpenRouter -> Gemini -> Claude SDK` chain. Rejected as premature and over-scoped. |
| [Issue #3829: OpenRouter has no failover](https://github.com/thedotmack/claude-mem/issues/3829) / [PR #3971](https://github.com/thedotmack/claude-mem/pull/3971) | Issue closed; PR merged | Shipped multiple OpenRouter model variants through OpenRouter's native fallback. It does not cross providers and is disabled for custom base URLs. |
| [Issue #3652: quota batches have no retry, dead-letter, or failover](https://github.com/thedotmack/claude-mem/issues/3652) | Closed as not planned; consolidated into open [plan #3606](https://github.com/thedotmack/claude-mem/issues/3606) | Explicitly asks to fail over to already-configured Gemini/OpenRouter when Claude quota is exhausted, or at least pause. The active plan preserves failed batches; it does not define a provider chain, and sends queue persistence/retry to plan #3609. |
| [PR #3941: rotate API keys on rate limit/quota](https://github.com/thedotmack/claude-mem/pull/3941) | Open | Automatic fallback among multiple keys for the selected Gemini or OpenRouter provider. The PR explicitly says it is neither cross-provider nor model fallback. |
| [Issue #3645: per-session/per-project provider and model routing](https://github.com/thedotmack/claude-mem/issues/3645) | Closed as not planned; consolidated into [plan #3607](https://github.com/thedotmack/claude-mem/issues/3607) | Requests dynamic provider/model selection based on the observed session. It is configuration-context routing, not quota- or power-driven fallback. |
| [Plan #2785: Provider & Extensibility Roadmap](https://github.com/thedotmack/claude-mem/issues/2785) | Open | The maintainer folded in support for 9router, an external gateway that already fans out across providers with automatic fallback. That is adjacent evidence for putting cross-provider routing in a gateway, not a native claude-mem chain. |

## Quota cooldown, retry, and recovery discussions

There is substantial related work around waiting through provider limits and preserving queued work:

- [Issue #3037](https://github.com/thedotmack/claude-mem/issues/3037) asked for pause/backoff until the parsed quota reset, followed by automatic queue drain. It was closed as fixed in `v13.9.1`: quota prose pauses generation instead of entering an infinite respawn loop.
- [Issue #3700](https://github.com/thedotmack/claude-mem/issues/3700) found that reactive Gemini/OpenRouter 429 errors still finalized sessions and dropped buffered work. Merged [PR #3999](https://github.com/thedotmack/claude-mem/pull/3999) now marks the session as quota-paused so the existing in-memory buffer is preserved.
- [Issue #3983](https://github.com/thedotmack/claude-mem/issues/3983) documents the current cooldown behavior: hooks continue entering the queue, generation is withheld, the cooldown is probed/re-armed at roughly 30-minute intervals, and the queue drains when the cooldown clears. Merged [PR #3986](https://github.com/thedotmack/claude-mem/pull/3986) exposes this state in observer health and SessionStart. The thread does not establish an independent wake-up timer when there is no later event attempting to start generation.
- [Issue #4068](https://github.com/thedotmack/claude-mem/issues/4068) is open and reports two current `13.24.23` failures: stale quota snapshots can keep re-arming the cooldown after reset, and restarting to recover discards the in-memory queue. Open [PR #4072](https://github.com/thedotmack/claude-mem/pull/4072) addresses stale quota windows but explicitly excludes restart queue loss.
- [Issue #4076](https://github.com/thedotmack/claude-mem/issues/4076) is the focused stale-cache report. Open [PR #4077](https://github.com/thedotmack/claude-mem/pull/4077) refreshes unified windows, expires reset buckets, and normalizes second/millisecond timestamps.
- Open [PR #4089](https://github.com/thedotmack/claude-mem/pull/4089) reports that a Gemini per-minute 429 with a six-second retry hint is currently misclassified as exhausted quota and turned into a repeating 30-minute cooldown. It proposes a 90-second recheck for rate limits and preserves the longer cooldown for actual daily/weekly/monthly exhaustion.
- [Discussion #2460](https://github.com/thedotmack/claude-mem/discussions/2460) proposes an operator requeue command for stranded server/BullMQ summary jobs. It is adjacent, not automatic local-worker retry.

So the repository already has the primitives and ongoing defects around "pause, keep work, retry after the limit," but no currently merged generic cross-provider fallback chain.

## Search scope

I fetched and searched the official repository's complete API listings available at review time: 2,090 issues, 1,728 pull requests, and 230 GitHub Discussions, including closed items and discussion comments. The provider-chain review covered provider/fallback/failover, OpenRouter, Gemini, model lists/variants, quota/rate limits, retry/backoff, cooldown, and requeue. The classifications above distinguish cross-provider routing from model or key fallback within one provider.

## Why this matters here

This is the evidence behind the router's premise: claude-mem has no merged cross-provider fallback, and its OpenRouter `models[]` support stops at the OpenRouter boundary. Routing therefore belongs in a gateway in front of claude-mem — which is what this repository is.

The same research also establishes the retry guarantee the [AC power gate](../../README.md) leans on: the provider enters a quota cooldown, the queue is preserved, and generation resumes when the cooldown clears. See `2026-09-20-claude-mem-retry-and-quota-limits.md` for the verification of that behavior in the installed plugin.
