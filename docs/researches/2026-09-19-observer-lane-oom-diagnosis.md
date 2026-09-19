# Is the local lane leaking memory, or is something else driving the Metal OOM?

Date: 2026-09-19

## Observed failures

The lane died four times with Metal allocation errors rather than a crash-on-clean-input:

```text
RuntimeError: [METAL] Command buffer execution failed: Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)
RuntimeError: [metal::malloc] Attempting to allocate 98784247808 bytes which is greater than the maximum allowed buffer size of 86586540032 bytes
```

Three of the four were the command-buffer variant; one was the absurd single allocation
(98,784,247,808 bytes against an 86,586,540,032-byte cap).

## Method

1. Read the prompt sizes claude-mem was actually sending.
2. Measure lane memory under a controlled stream of requests, with a fixed cache bound.
3. Compare against the allocation the model would need for the observed input.
4. Check whether the cache byte cap was doing what its name suggests.

## Evidence

- **Prompts were enormous.** claude-mem fields reaching the lane measured 3,250,882,
  2,848,518, 2,336,858, 2,225,670, and 1,198,634 characters.
- **The arithmetic matches the failure.** At roughly 128 KB of KV per token, a 3.25M-character
  prompt is on the order of 754k tokens, needing ~92 GiB of KV against a ~86.6 GB ceiling.
  The lane was asked for an allocation the machine cannot satisfy.
- **No leak under normal traffic.** In a controlled test of 20 unique streaming requests, RSS
  moved 2.71 GB → 2.73 GB (+20 MB) and stayed flat after request 5. The prompt cache held at
  4 sequences / 0.22 GB, and idle CPU was 0%.
- **The byte cap was not the effective bound.** `--prompt-cache-bytes` is not enforced in
  mlx_lm's streaming insertion path; the sequence count (`--prompt-cache-size 4`) is what
  actually bounds retention. That matters because an oversized *retained* prompt is what
  makes the next allocation impossible.

## Conclusion

The lane was not leaking. A single oversized prompt drives an allocation that cannot fit, so
the fix is to bound the request before it reaches any provider, not to tune the cache. That
conclusion produced the deterministic prompt-size guard described in the
[README](../../README.md#prompt-safety-and-context-folding).

## Verification of the fix

| Check | Result |
| --- | --- |
| Production-sized failure replay | 3,250,938 input chars → 97 chars; one binary blob removed; HTTP 200 streaming in 2.04 s |
| Local lane after replay | 200 in 0.21 s; no generation-thread failure |

## Residual risk

The guard blocks the trigger observed here. It does not make the lane immune to an unrelated
MLX generation-thread failure, and it does not raise the model's usable window — see
`2026-09-19-yarn-vs-kv-memory.md` for why a longer positional window would not have helped.
