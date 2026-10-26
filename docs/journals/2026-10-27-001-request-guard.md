# 2026-10-27-001 — request guard

## 00:30 — Host and Content-Type checks from the security review

- Premise: a whole-project security review found no HIGH or MEDIUM issue, and two items below the
  bar: the router does not check `Host` or `Content-Type`, so a web page could send it requests
  or, through DNS rebinding, read its answers; and the service log is readable by every local
  account. Impact was limited to spending the owner's free quotas and reading `/health` metadata,
  but both checks are cheap.
- Outcome: `Host` must be `localhost`, an IP literal, or a name in
  `OBSERVER_ROUTER_ALLOWED_HOSTS`; otherwise `403`. IP literals are accepted because a rebound
  page can only carry a DNS name, and this keeps LAN access by address working without a setting.
  A POST must declare `application/json` or gets `415`. claude-mem 15.2.1 sends that header.
- Found while testing: a refused POST left its body unread on a keep-alive connection, and the
  server parsed the body as the next request (a `Bad request version` error and a broken pipe).
  Refusals now send `Connection: close`
  (`RequestGuardTests.test_a_refusal_closes_the_connection`).
- Not changed: the log permissions. launchd creates the file, the machine has one human account,
  and moving it is a deployment choice; `docs/reference/security.md` says how.
- References: `RequestGuardTests`, and the `allowed host that is not a name` case in
  `EnvironmentTests`.

## 01:00 — Review: the unread body was a real bypass

- Outcome: an independent review noted that the 404 for a POST to an unknown path did not close
  the connection either. That is not only tidiness. A page may post `text/plain` to
  `http://127.0.0.1:1244/anything` (an IP literal passes the Host check); the router answered 404
  and left the body unread, then parsed it as the next request, so a complete JSON POST inside it
  reached a provider and spent quota, past the Content-Type guard. Reproduced in
  `test_a_body_left_unread_is_never_parsed_as_the_next_request` before the fix: two responses
  instead of one, and the upstream saw the inner request. Every error sent before the body is
  read now goes through one `_refuse`, which closes the connection, including a `400` for a
  `Content-Length` that is not a non-negative integer (it used to be read as empty).
- Outcome: `localhost.` is accepted as `localhost`. Tests added for media types that only start
  like JSON, a trailing dot, and the list parsing of `OBSERVER_ROUTER_ALLOWED_HOSTS`.
- Not changed: a malformed bracketed `Host` such as `[evil]:80`, an empty one, and a bad port are
  still accepted. A browser cannot send them, and a rebound page always carries its DNS name, so
  none is a bypass; `docs/reference/security.md` now says they are not checked.
