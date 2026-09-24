# 2026-09-24-003 — local tier waits for idle interactive lanes

## 15:40 — which Splash metric means "busy"

- Outcome: `splash_frontend_active` stayed `0` while a request was decoding on `:1242`, so it
  does not indicate activity. `splash_scheduler_decoding` did, but only while a sample landed
  inside the request. The gate uses
  `submitted − completed − cancelled − failed` plus any change in the submitted counter, which
  also catches a request shorter than the sample interval.
- References: `curl http://127.0.0.1:1242/metrics` sampled every 0.5 s during a request.

## 15:42 — gate verified and enabled

- Outcome: a throwaway instance on `:1247` watching `:1240` and `:1241` with a 10 s grace
  served from the local lane while both were idle, answered
  `local(skipped: lanes busy (127.0.0.1:1241, idle 10s required))` 2.5 s after a request to
  `:1241`, and served again once the grace had passed. Production now watches `:1240` and
  `:1241` with the 30 s default.
- References: `observer-router.py` (idle gate section), `test_observer_router.py` (31 tests),
  `~/Library/LaunchAgents/com.ezou.observer-router.plist`.
