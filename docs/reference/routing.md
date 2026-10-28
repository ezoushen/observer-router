# Routing and streaming

What counts as an answer, what counts as a failed attempt, how a repeated request is routed, and
how a streamed reply commits.

## Tier behaviour

- **`openrouter/free` can return empty content.** It rotates across free variants, and a
  reasoning model can spend the whole `max_tokens` budget on `reasoning`
  (`finish_reason: length`, `content: ""`). The router injects
  `{"reasoning":{"enabled":false}}`.
- **That injection can be refused.** An endpoint may answer
  `HTTP 400 "Reasoning is mandatory for this endpoint and cannot be disabled."`, so the router
  retries the same tier once with `{"reasoning":{"exclude":true}}`.
- **A tier with no content is a failure, not a result.** Streaming or not, an empty answer
  falls through to the next tier, so claude-mem never records an empty observation.
- **A reply cut off at the token limit is a failure too.** See
  [below](#replies-cut-off-at-the-token-limit).
- **Reasoning deltas are dropped.** Only `delta.content` is forwarded downstream.
- **A reply from a rejected model is a failure too.** See [below](#rejected-models).
- **A bridge instance authenticates to the bridge, not to the service behind it.** Point its
  `api_key_file` at the bridge's own env file (for cursor-api-proxy, `CURSOR_BRIDGE_API_KEY`),
  so the secret has one copy. A configured key source that yields no key fails the instance.
- **An observer skip looks like a failure.** claude-mem's prompt asks for an empty response
  when a tool call is routine. A tier that obeys returns empty content, so the next tier runs
  and may record what the first one skipped.

## Replies cut off at the token limit

`finish_reason: length` means the text stopped mid-thought, or a model looped until its cap.

- **Non-streaming**, the next tier answers.
- **Streaming**, only an instance with `buffer_stream = true` can fail over
  ([buffered streams](#buffered-streams)); any other has already forwarded its text, so the
  stream ends with a `finish_reason: length` frame and the client knows.
- **When no later tier answers**, the first cut-off reply is served after all, with
  `finish_reason: length`. claude-mem keeps the complete blocks of a cut-off reply, so part of an
  answer beats a 502.
- **One log line** per cut-off, `tier <name> cut off at the token limit: ...`, records
  `prompt_tokens`, `prompt_chars`, `completion_tokens`, and the first and last 80 characters, so
  a loop can be told from a genuinely long answer. Those characters are model output, kept in the
  service log.
- **Not a breaker failure.** A cut-off depends on the prompt, not the provider's health: a prompt
  that makes a model loop costs that tier's attempt each time it is sent, but cannot block the
  tier for other prompts.

Raising the server's token cap is not the fix: a loop only runs longer.

## Rejected models

A provider that picks the model itself, such as `openrouter/free`, can land on one that cannot do
the job: a safety classifier answers every prompt with `User Safety: safe`. A provider's
`reject_models` lists model ids, matched exactly against the `model` the reply names. A reply from
one of them is a failed attempt, like an empty one: it counts toward the provider's breaker, and
the next tier answers. The check runs on the first frame that names the model, so a rejected
stream is closed without waiting for the rest of it; OpenRouter names it on every frame, so no
text reaches the client first. A stream that names its model only after its first text has
committed by then, and ends as a failed stream. The rule names models, not content, so it holds
for any client.

## Repeated requests

A client that sends a request again after getting an answer did not take that answer: its own
check refused the reply, and it is asking again. The same chain would give the identical request
to the same model, which tends to make the same mistake every time. So the router remembers, for
`OBSERVER_REPEAT_WINDOW_SECONDS` (600 s) after each answer, which chain entries answered a request:

- **An identical request tries the other entries first**, in chain order. The entries that
  answered it follow, the one that answered longest ago first, so repeats rotate through the
  chain.
- **They are tried last, not skipped.** When every other entry fails, one of them still answers.
- **A group counts as one entry**: its members serve one model, so another key would give the
  same answer.
- **Identical means the same JSON body**, field order aside. Any difference, including a changed
  prompt or temperature, makes a new request.

A client that resends a request because it stopped waiting, not because it refused the answer,
also gets the other entries first; their failed attempts count against its timeout. The router
never reads the reply to judge it, so this holds for any client. The service log shows
`repeated request: <entries> answered it already, tried last`. `0` turns the rule off.

## The request deadline

A request has one deadline, `OBSERVER_REQUEST_DEADLINE_SECONDS` (85 s) after it arrived. A client
stops waiting at some point, and a reply produced after that reaches no one while it holds the
upstream, so the whole chain works inside the deadline:

- **Each attempt gets what is left**, at most its `budget_s`, so time spent failing on earlier
  tiers comes out of later ones.
- **A provider is skipped** when less than its `min_budget_s` (default 1 s) is left. It is not
  called, and the skip is not a breaker failure.
- **Waiting for a `serial` lane counts.** A request waits for a lane held by another one only until
  `min_budget_s` would be left, then moves on to the next tier; that skip is not a breaker failure
  either.
- **When no tier can answer in time**, the router returns its `502` with `attempted`, before the
  client gives up, so claude-mem retries instead of timing out.

The default sits under claude-mem's 90 s stream idle timeout, which counts from the request until
the router's headers arrive. A buffered provider's `min_budget_s` is worth sizing from its typical
whole-reply time: with less left, the reply would most likely be cut off by the deadline anyway.
The `served` log line carries `latency`, from the answering tier's start, and `age`, from the
request's arrival, which is what the client's timeout measures.

## Streaming contract

claude-mem sends `stream=true`, so the router speaks SSE with chunked transfer encoding.
Response headers are deliberately withheld until the **first content-bearing delta** arrives;
that keeps failover possible right up to the moment the stream commits. An upstream that sends its
own headers and then stalls past its budget is a failed attempt, like one that never answers.
Once committed, a mid-stream failure cannot switch tiers — it is logged and the stream is closed
with `data: [DONE]`.

## Buffered streams

An instance with `buffer_stream = true` commits only when its upstream stream has ended. Its text
is held back until `finish_reason` shows the reply is whole, so a reply cut off at the token
limit, a stream that ends without a `finish_reason`, a read error at any point, or a reply still
unfinished when its `budget_s` runs out, still fails over. The budget is a hard bound on the whole
reply, from the moment the request is sent, so it also frees a `serial` lane that would otherwise
keep generating for a client that has given up; size it from that client's 90 s idle timeout, not
from prefill time alone. The cost is latency: claude-mem receives its first byte only once the
whole reply is generated, and that must happen inside its 90 s stream idle timeout. Turn it on for
a small local model with a hard output cap, where a cut-off is common and a full reply takes
seconds; leave it off for fast hosted tiers. Without it, a cut-off stream has already been
forwarded: it is logged and ends with `finish_reason: length`, and is never recorded as served.
