# Prompt guard and context folding

Every request is sanitized once before provider selection, so every provider receives the same
bounded conversation:

- OpenAI image parts and `data:image/...;base64,...` payloads become descriptive markers.
- Contiguous base64-like blobs of at least 16,384 characters become markers.
- Any remaining field over 80,000 characters keeps its beginning and end around a folding
  marker.
- If the conversation still exceeds 240,000 characters (roughly 60k tokens), the primary
  system message and newest non-system message receive first claim on the budget, followed by
  remaining system messages and recent history. Older messages are dropped last. An oversized
  system message may not take the whole budget: up to half is held for the newest message.

This is deterministic: compaction never calls another model, so it still works when every
upstream quota is exhausted. `/health` reports cumulative compaction counters and the active
limits. A log entry records input/output characters, dropped messages, folded fields, images,
and binary blobs for each changed request.

The guard exists because an unbounded prompt is what actually killed the lane — not a leak.
See [the diagnosis](../researches/2026-09-19-observer-lane-oom-diagnosis.md).

The limits are environment settings: `OBSERVER_MAX_PROMPT_CHARS`, `OBSERVER_MAX_FIELD_CHARS`,
`OBSERVER_MIN_RETAINED_CHARS`, and `OBSERVER_BASE64_BLOB_CHARS`
([configuration](configuration.md#environment)).

## Sizing a limit for one provider

The limits above are router-wide. A provider whose model has a smaller context window can set
`max_prompt_chars` in its config table: its requests are folded again, deterministically and by
the same rules, to that size, while every other provider still gets the full prompt. It can only
tighten the router-wide guard, never raise it, and a prompt already under it is untouched. The
smallest value accepted is twice `OBSERVER_MIN_RETAINED_CHARS` (2,048 by default), enough to keep
a system message and the request; a value above the router-wide limit never acts, and start-up
logs a warning for it. Each
fold is logged as `compacted prompt for tier <name> chars=a->b`.

Ask the lane for its window instead of guessing. An OpenAI-compatible server lists it with the
model:

```bash
curl -s http://<lane>/v1/models | python3 -m json.tool   # look for context_length / max_model_len
```

Locals, for example, answers `"context_length": 32768`. That is tokens; the reply cap is not
listed (Locals takes it from `--max-tokens`, 2048 in the reference setup), and the 400 it returns
for an oversized prompt only states the window. Then:

```text
max_prompt_chars  =  (context_tokens - reply_cap) * chars_per_token
```

Observer prompts measured 3.5 to 4.4 characters per token, so use about 3.5 to stay safe:
`(32768 - 2048) * 3.5` is about 107,000, and 96,000 leaves a margin. A server that does not list
its window may report the prompt token count of each reply (`usage.prompt_tokens`), which with the
prompt's character count gives the ratio.
