# Known limitations

1. **Free hosted quotas run out.** Free Gemini and OpenRouter keys have hard daily caps, so a
   chain of free tiers alone goes dark for the rest of the day; end it with an instance that has
   no such cap (a local lane, a paid key, or a bridge).
2. **A hung-but-alive local lane still defeats launchd `KeepAlive`.** A lane can keep answering
   `/v1/models` after its generation thread is gone, so the process still looks alive to launchd.
   The router fails over around it, but cannot restart it; that still needs
   `launchctl kickstart -k` on the lane's job.
3. **Cosmetic log noise.** claude-mem client disconnects raise `BrokenPipeError` and
   `ConnectionResetError` tracebacks in the router log. They are harmless but loud.
4. **A buffered instance answers only when its reply is whole.** Under load, the wait for a
   `serial` lane plus a whole reply can pass claude-mem's 90 s stream idle timeout, which a
   streamed first token would have reset. Sending headers and keep-alive comments early would
   avoid it, at the cost of the failed-chain 502.
5. **Unbuffered tiers still forward a cut-off reply.** Hosted tiers that hit claude-mem's
   `max_tokens` (seen while a model echoes the file it observes) cannot fail over; the reply is
   logged, ends with `finish_reason: length`, and claude-mem keeps its complete blocks.
6. **Breakers and quota blocks live in memory.** A restart forgets them, so the first requests
   after one meet each exhausted quota again before it is blocked anew.
