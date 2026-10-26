# Observer router

One OpenAI-compatible endpoint in front of the providers you choose. You list provider instances
in a TOML file and put them in the order to try; each request is answered by the first one that
returns usable content. Built for claude-mem's observer calls, which name exactly one provider
and have no fallback of their own, but any OpenAI-compatible client works.

- **Any chain you want:** Gemini, OpenRouter, and any OpenAI-compatible server (local lanes,
  bridges), several instances per type, in any order.
- **Key pools:** a group of keys rotates across requests and fails over within itself.
- **Failure-aware:** errors, timeouts, empty replies, and replies cut off at the token limit
  move on to the next provider; quota 429s block a key until its stated reset.
- **Bounded prompts:** every request is folded to a fixed size before any provider sees it.
- **One file, standard library only:** nothing to install, so it starts even when the network
  or a package index is down.

## Quick start

You need Python 3.11 or later. Run the commands from the repository root.

1. **Write a config.** Copy the annotated example, which documents every field:

   ```bash
   mkdir -p ~/.config/observer-router
   cp config.example.toml ~/.config/observer-router/config.toml
   ```

   Then replace its contents with your own providers and `chain`. A minimal chain, one Gemini
   key with a local OpenAI-compatible server behind it:

   ```toml
   chain = ["gemini", "local"]

   [providers.gemini]
   type = "gemini"
   model = "gemini-flash-lite-latest"
   api_key_env = "GEMINI_API_KEY"      # the key stays in the environment, never in this file

   [providers.local]
   type = "openai"
   url = "http://127.0.0.1:8080/v1/chat/completions"
   model = "my-local-model"            # the id the server lists at /v1/models
   ```

2. **Start it.** It runs in the foreground and logs to the terminal; a bad config or setting
   stops it at once, with the reason. The key is a Gemini API key from Google AI Studio.

   ```bash
   GEMINI_API_KEY=your-key python3 observer-router.py
   ```

   In a second terminal, check it: `curl -fsS http://127.0.0.1:1244/health` shows the chain.

3. **Send a request.** The `model` you send is an alias; each provider gets its own. If no
   provider answers (a wrong key, nothing listening at the local URL), the reply is a 502 whose
   `attempted` list says why each one failed.

   ```bash
   curl -sS http://127.0.0.1:1244/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model": "observer-router", "messages": [{"role": "user", "content": "Say hi"}]}'
   ```

4. **Point your client at it:** base URL `http://127.0.0.1:1244/v1`. For claude-mem, see
   [Use with claude-mem](docs/guides/claude-mem.md).

5. **Keep it running** as a macOS service: [Install as a macOS service](docs/guides/install.md).

## Documentation

| Page | Read it for |
| --- | --- |
| [Build your chain](docs/guides/build-a-chain.md) | Providers, order, key pools, per-provider behaviour, budgets |
| [Configuration](docs/reference/configuration.md) | Every config field and environment setting |
| [Routing and streaming](docs/reference/routing.md) | What counts as a failed attempt, cut-off replies, streaming |
| [Breakers, quota blocks, and group blocks](docs/reference/breakers-and-quotas.md) | When a provider is skipped, and for how long |
| [Operate the router](docs/guides/operations.md) | Health, logs, testing one provider alone |
| [All documentation](docs/README.md) | Every guide and reference page, plus research, plans, and the work journal |

## Development

```bash
python3 -m unittest -v test_observer_router.py                           # tests
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # once: the markdown linter
scripts/lint-md                                                          # markdown lint
```

Conventions for contributors and coding agents are in [`AGENTS.md`](AGENTS.md); notable changes
are in [`CHANGELOG.md`](CHANGELOG.md).
