# 2026-09-29-002 — rotating provider groups

## 02:10 — Groups rotate keys instead of stacking them as fallbacks

- Outcome: five Gemini keys were verified without printing any value. All five were present and
  distinct, and each answered 200 on the model-list endpoint, which spends no generation quota.
- Outcome: `[groups.<name>] members = [...]` lets a chain entry rotate. The start member advances
  per request and the other members follow, so a quota-blocked key is skipped instantly and load
  spreads across the pool.
- Outcome: the handler originally expanded the chain twice per request, and with an even pool
  every request then started at the same member. A test that spreads four requests across two
  fake upstreams fails on that mutation; the chain is now expanded once per request.
- References: `ConfigTests.test_a_group_rotates_its_members_across_requests`,
  `ChainTests.test_a_group_spreads_consecutive_requests_across_its_members`.
