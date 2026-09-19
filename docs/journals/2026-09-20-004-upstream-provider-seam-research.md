# 2026-09-20-004 — upstream survey and provider-resolution plan

## 01:48 — claude-mem's provider extensibility surveyed; a narrow seam plan written

- Outcome: surveyed upstream `thedotmack/claude-mem` for a "model selection policy" override
  point by enumerating all 42 open issues and 183 open pull requests, keyword-searching every
  issue and PR in any state, reading the maintainer's roadmap masters (`plans/12`, `plans/18`,
  `plans/19`) and the published custom-backend documentation page, and comparing with prior art
  from LiteLLM, OpenRouter, claude-code-router, CC-Router, vLLM Semantic Router, Aegis gateway
  and the llm-d payload processor. Wrote the survey as a research record and a complete plan
  with a ready-to-file draft, an evidence pack, risks and decision points.
- Findings: the override point the idea asks for **largely already exists** — `ANTHROPIC_BASE_URL`
  and `CLAUDE_MEM_OPENROUTER_BASE_URL` are both documented seams, and the docs explicitly
  recommend putting a gateway such as LiteLLM in front for OpenAI-only backends. Multi-account
  rotation is **already in flight** as PR #3941 (a per-provider key pool with kind-specific
  cooldowns), routed by the maintainer to plan-12 and stacked under PR #3942 (a first-class
  generic `openai-compatible` provider). Two measured gateway adapter defects recorded in #2785
  already have a fix in PR #3668. Nothing open proposes a per-request resolution seam, but
  #680 (provider chain) was closed as premature and over-scoped, and its review left an explicit
  door open: fallback "could be reconsidered with evidence of regular quota exhaustion" — which
  now exists.
- Decision: do not file a broad "policy / provider chain" issue. The defensible narrow ask is one
  provider-neutral resolution point that generalizes #3941 and #3942, filed either as a new RFC
  or as a comment on #2785 (plan-12, Providers & auth). Both routes are drafted; the owner picks.
- Open question flagged: citing this repository publicly as the reference implementation requires
  flipping `ezoushen/observer-router` from private to public.
- References: `docs/researches/2026-09-20-claude-mem-provider-extensibility-survey.md`,
  `docs/plans/2026-09-20-claude-mem-provider-resolution-seam.md`, `docs/plans/README.md`
