# Thinking is off by locals' default, not by the router's config

2026-10-29, correcting [2026-10-29-001](2026-10-29-001-mbp14-lfm3-cutover.md). That entry says the lane
runs "thinking off through the template", implying the router's
`chat_template_kwargs = { enable_thinking = false }` is what turns it off. It is not.

## What the lane actually does

`locals` denies a client-set `enable_thinking`: the key sits in `_TEMPLATE_DENY` beside `chat_template`,
because a client-supplied template would run server-side. The value is therefore dropped from every
request this router sends, and thinking stays off because `locals` defaults it off for this template.

Thinking is selected two other ways:

- a model id ending in `-thinking`, which `check_model` strips and `chat_model` re-adds when the
  template offers thinking. `/v1/models` lists that variant only for models that can think;
- the server-wide `--thinking` flag.

Measured on the lane with one prompt: the plain id produced 104 tokens and a wrong answer, the
`-thinking` id produced 210 tokens and the correct one. Same weights, both without a `reasoning` field
in the response.

## Why it matters

- The router's config line is **inert** on this lane. It stays, because it would take effect on a lane
  served by plain `mlx_lm.server`, which does honour the key — and the comment in the config now says
  exactly that instead of claiming a behaviour the lane does not have.
- Nothing changes for claude-mem traffic: the lane is pointed at the plain id, which is what observer
  and summary work wants. The `-thinking` variant roughly doubles the token count and would waste the
  budget.
- The evaluation numbers stand: every harness run was made against the plain id, where thinking is off
  for the same reason it is off in production.

## Verified

The lane answers both ids from `/v1/models`, `/health` is 200, the router reports the chain intact with
the unchanged model id, and the config edit was a comment only — no restart was needed and none was
made.
