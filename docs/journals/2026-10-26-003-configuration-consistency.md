# 2026-10-26-003 — configuration consistency

## 23:30 — The README describes building a chain; two knobs join the config

- Request: the README should not state this deployment's chain, should show how to build any
  chain, and everything should be configurable as designed: per-instance behaviour in the TOML
  file, router-wide policy in `OBSERVER_*` variables, nothing server-specific in code.
- Audit: four mismatches. `buffer_stream` was accepted only on `openai` providers, although it is
  a stream policy and hosted tiers reach the token limit too. The group-block threshold was a
  fixed 2 beside the configurable breaker settings. The idle gate reads only Splash's counter
  names. OpenRouter always gets reasoning disabled, with no per-instance switch.
- Decision (owner): fix the first two; leave the idle-gate counters and the OpenRouter reasoning
  switch until a lane or a model needs them. The README now names the Splash counters.
- Outcome: `buffer_stream` is a common field, default off. `OBSERVER_GROUP_STRIKES`, default 2,
  sets the threshold. The README leads with providers, order, groups, per-instance behaviour and
  router-wide policy, with two example chains, and no longer describes the reference deployment.
- References: `ChainTests.test_a_buffered_hosted_provider_fails_over_on_a_cut_off_stream`,
  `test_the_group_strike_threshold_is_configurable`.

## 23:45 — Environment settings are validated

- Outcome: an `OBSERVER_*` value that is not a finite number, or is out of range, now stops the
  router at start-up with the variable named (exit status 2), as an invalid config file does.
  Before, a non-number silently became the default, and values such as
  `OBSERVER_GROUP_STRIKES=0` or a negative breaker interval were taken as given. Unset or empty
  still means the default. The production plist sets no `OBSERVER_*` variable.
- References: `EnvironmentTests.test_an_invalid_setting_stops_the_router_and_names_it`.
- Review: an integer too large for a float crashed the check itself (`OverflowError`), and an
  `OBSERVER_BASE64_BLOB_CHARS` past the regex repeat limit crashed the import. Integer settings
  now stop at 1,000,000,000, `OBSERVER_IDLE_SAMPLE_SECONDS` at 3600, and the integer check never
  converts to float; each case is in the test.
