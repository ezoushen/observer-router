# Configuration reference

## Config file

The router reads one TOML file: `OBSERVER_ROUTER_CONFIG`, default
`~/.config/observer-router/config.toml`. Start from [`config.example.toml`](../../config.example.toml),
which documents every field. A missing or invalid file stops the router at start-up with the
reason in the log (exit status 2); it never starts with a partial chain.

```toml
chain = ["gemini-a", "gemini-b", "local"]

[providers.gemini-a]
type = "gemini"
model = "gemini-flash-lite-latest"
api_key_env = "GEMINI_API_KEY_A"

[providers.gemini-b]
type = "gemini"
model = "gemini-flash-lite-latest"
api_key_file = "~/.config/observer-router/secrets.env"
api_key_var = "GEMINI_API_KEY_B"

[providers.local]
type = "openai"
url = "http://127.0.0.1:1240/v1/chat/completions"
model = "local-model"
serial = true
ac_only = true
```

| Field | Types | Default | Meaning |
| --- | --- | --- | --- |
| `type` | all | required | `gemini`, `openrouter`, or `openai` |
| `model` | all | required | Model id sent upstream; for a local server, the id it advertises at `/v1/models` |
| `url` | all | per type; required for `openai` | Chat-completions URL, `http` or `https` |
| `budget_s` | all | 18 / 24 / 40 | Seconds allowed to reach the first content delta; with `buffer_stream`, to finish the whole reply |
| `api_key_env` | all | none | Environment variable holding the key; checked first |
| `api_key_file`, `api_key_var` | all | none | A dotenv file (`NAME=value`), or a JSON object when the path ends `.json`, and the key's name in it; re-read on every request |
| `site_url`, `app_name` | `openrouter` | empty, `observer-router` | `HTTP-Referer` and `X-Title` attribution |
| `reasoning_effort` | `openai` | unset | Sent as-is (`"none"` turns Splash thinking off); leave unset for servers that reject unknown fields |
| `chat_template_kwargs` | `openai` | unset | Sent as-is, e.g. `{ enable_thinking = false }` for Qwen on mtplx or vLLM, which ignore `reasoning_effort` |
| `serial` | `openai` | `false` | One request at a time per lane URL |
| `ac_only` | `openai` | `false` | Skip while on battery |
| `buffer_stream` | all | `false` | Hold a stream back until it ends, so a reply cut off at the token limit fails over |
| `idle_metrics`, `idle_seconds` | `openai` | none, `30` | Wait for these lanes to be idle first |
| `max_prompt_chars` | all | router-wide `OBSERVER_MAX_PROMPT_CHARS` | Fold the prompt to this many characters for this provider only; can only tighten the router-wide guard ([sizing](prompt-guard.md#sizing-a-limit-for-one-provider)) |
| `break_after` | all | router-wide `OBSERVER_BREAK_AFTER` | Failures in a row before this provider is skipped |
| `break_seconds` | all | router-wide `OBSERVER_BREAK_SECONDS` | How long it is skipped, by its own breaker or a group block |

A provider's `max_prompt_chars`, `break_after` and `break_seconds` override the router-wide
settings of the same meaning; `/health` shows the ones that are set, so a restart can be checked.
The file is read at start-up, so a change needs a restart
([how to restart](../guides/install.md)), not a new release.

`chain` lists provider or group names in order; without it the providers' order is used, and
groups then go unused. A group
is a `[groups.<name>]` table with one field, `members`, a list of provider names; its name must not
also be a provider's, groups do not nest, and no provider may be reachable twice through the chain.

```toml
chain = ["gemini", "openrouter", "local"]

[groups.gemini]
members = ["gemini-a", "gemini-b", "gemini-c"]   # request 1 starts at a, request 2 at b, ...
```

The rotation position is in memory and starts again at the first member after a restart. `/health`
lists each group's members under `groups`, and every member's breaker under `breaker`. `gemini` and
`openrouter` instances must name a key source. **Keys never go in the file**: an inline `api_key`
is rejected, as is any field the instance's type does not know, so a typo cannot be ignored
silently. Every value is type-checked at start-up (`serial = "false"` is an error, not true), and
a key that turns out to contain control characters fails its instance without being echoed.
Environment variables from before the config file (`OBSERVER_LOCAL_*`, `OBSERVER_CURSOR_*`, and
the like) are ignored, and start-up logs their names. `config.toml` and `*.env` are git-ignored
in this repository.

## Environment

Environment overrides, all optional. An unset or empty variable takes the default. A value that is
not a number, or is out of range, stops the router at start-up with the variable named in the
log (exit status 2), as an invalid config file does; it never falls back to the default silently.
Integer settings stop at 1,000,000,000.

| Variable | Default | Accepts |
| --- | --- | --- |
| `OBSERVER_ROUTER_CONFIG` | `~/.config/observer-router/config.toml` | a path |
| `OBSERVER_ROUTER_CHAIN` | the file's `chain` | comma-separated provider or group names, e.g. to drop a local lane during a benchmark |
| `OBSERVER_ROUTER_HOST` / `OBSERVER_ROUTER_PORT` | `127.0.0.1` / `1244` | an address / an integer 1–65535 |
| `OBSERVER_ROUTER_ALLOWED_HOSTS` | none | comma-separated DNS names, besides `localhost` and IP literals, accepted in a request's `Host` header; see [security](security.md) |
| `OBSERVER_BREAK_AFTER` / `OBSERVER_BREAK_SECONDS` | `3` / `60` | an integer ≥ 1 / seconds ≥ 1 |
| `OBSERVER_GROUP_STRIKES` | `2` | an integer ≥ 1: group members failing in a row, by timeout or unexplained 429, that block the group |
| `OBSERVER_POWER_CACHE_SECONDS` | `30` | seconds ≥ 0 |
| `OBSERVER_IDLE_SAMPLE_SECONDS` | `2` | seconds, 0.1–3600 |
| `OBSERVER_MAX_PROMPT_CHARS` / `OBSERVER_MAX_FIELD_CHARS` | `240000` / `80000` | integers ≥ 1 |
| `OBSERVER_MIN_RETAINED_CHARS` | `1024` | an integer ≥ 0 |
| `OBSERVER_BASE64_BLOB_CHARS` | `16384` | an integer ≥ 1 |
