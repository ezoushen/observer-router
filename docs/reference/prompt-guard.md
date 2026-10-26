# Prompt guard and context folding

Every request is sanitized once before provider selection, so every provider receives the same
bounded conversation:

- OpenAI image parts and `data:image/...;base64,...` payloads become descriptive markers.
- Contiguous base64-like blobs of at least 16,384 characters become markers.
- Any remaining field over 80,000 characters keeps its beginning and end around a folding
  marker.
- If the conversation still exceeds 240,000 characters (roughly 60k tokens), the primary
  system message and newest non-system message receive first claim on the budget, followed by
  remaining system messages and recent history. Older messages are dropped last.

This is deterministic: compaction never calls another model, so it still works when every
upstream quota is exhausted. `/health` reports cumulative compaction counters and the active
limits. A log entry records input/output characters, dropped messages, folded fields, images,
and binary blobs for each changed request.

The guard exists because an unbounded prompt is what actually killed the lane — not a leak.
See [the diagnosis](../researches/2026-09-19-observer-lane-oom-diagnosis.md).

The limits are environment settings: `OBSERVER_MAX_PROMPT_CHARS`, `OBSERVER_MAX_FIELD_CHARS`,
`OBSERVER_MIN_RETAINED_CHARS`, and `OBSERVER_BASE64_BLOB_CHARS`
([configuration](configuration.md#environment)).
