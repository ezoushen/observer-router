# Security model

The router has no authentication. It is meant to be reached by software on the same machine, and
what it protects are the API keys it holds and the quotas behind them.

## What it does

- **Listens on loopback.** The default bind is `127.0.0.1`. Binding wider is the operator's
  choice (`OBSERVER_ROUTER_HOST`); anything that can reach the port can then spend the quotas.
- **Keeps keys out of reach.** Keys come only from the environment or a key file named in the
  config, never from a request. Clients cannot choose the upstream host or the `model`, no
  client header is forwarded, and keys never appear in logs, `/health`, or error bodies.
- **Refuses requests from web pages.** A page in a browser on the same machine can send requests
  to `127.0.0.1`, and a rebound DNS name can even read the answers. Two checks stop that:
  - the `Host` header must be `localhost`, an IP literal, or a name in
    `OBSERVER_ROUTER_ALLOWED_HOSTS` (a trailing dot is ignored); any other name is answered
    `403`. An IP literal cannot be rebound, so LAN access by address needs no setting. A browser
    always sends a well-formed `Host`, so a missing or malformed one is not checked;
  - a `POST` must declare `Content-Type: application/json` (parameters such as `charset` are
    fine), or it is answered `415`. A cross-site form or text post cannot declare JSON without a
    CORS preflight, and the router answers no `OPTIONS` request and sends no CORS headers.
  - Each refusal is logged (`refused 403 GET /health: Host='...'`), so a legitimate client that
    is turned away can be found.
  - Every error answered before the body is read (`403`, `415`, `404` for an unknown path, and
    `400` for an unusable `Content-Length`) also closes the connection. Otherwise the unread body
    would be parsed as the next request, and a page that may only post text could smuggle in a
    POST that looks like JSON.
- **Executes nothing from input.** Config is parsed as TOML only, and the one subprocess
  (`pmset`) takes a fixed argument list.

## What it leaves to you

- **The log is readable by every local account.** launchd writes `/tmp/observer-router.log`
  with default permissions, and it holds short previews of model output when a reply is cut off
  at the token limit ([routing](routing.md#replies-cut-off-at-the-token-limit)). On a machine
  with more than one person, point `StandardOutPath` and `StandardErrorPath` in the plist at a
  private directory.
- **Quotas are shared by everything that can reach the port.** On a shared network, bind to
  loopback or put the router behind something that authenticates.
