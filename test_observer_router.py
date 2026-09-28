#!/usr/bin/env python3
"""Contract tests for observer-router: prompt compaction, the power and idle gates, the cursor
tier, and quota-reset blocks."""

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from email.message import Message
from io import BytesIO
from typing import Any
from urllib.error import HTTPError


MODULE_PATH = Path(__file__).with_name("observer-router.py")
SPEC = importlib.util.spec_from_file_location("observer_router", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {MODULE_PATH}")
router: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(router)


def _config(text: str) -> Any:
    """Parse a TOML config through the public loader, via a temporary file."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(text)
        return router.load_config(str(path))


TWO_GEMINIS = """
chain = ["gemini-b", "gemini-a", "local"]

[providers.gemini-a]
type = "gemini"
model = "gemini-flash-lite-latest"
api_key_env = "TEST_GEMINI_A"

[providers.gemini-b]
type = "gemini"
model = "gemini-2.5-flash"
api_key_env = "TEST_GEMINI_B"
budget_s = 12

[providers.local]
type = "openai"
url = "http://127.0.0.1:1242/v1/chat/completions"
model = "local-model"
"""


class ConfigTests(unittest.TestCase):
    """The config file defines named provider instances and the order they are tried in."""

    def test_chain_names_instances_in_the_configured_order(self):
        config = _config(TWO_GEMINIS)

        self.assertEqual(config.chain, ("gemini-b", "gemini-a", "local"))
        self.assertEqual(config.providers["gemini-a"].type, "gemini")
        self.assertEqual(config.providers["gemini-b"].model, "gemini-2.5-flash")
        self.assertEqual(config.providers["gemini-b"].budget, 12.0)

    def test_type_defaults_fill_url_and_budget(self):
        config = _config(TWO_GEMINIS)
        gemini = config.providers["gemini-a"]

        self.assertEqual(
            gemini.url, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        self.assertEqual(gemini.budget, 18.0)
        self.assertEqual(config.providers["local"].url, "http://127.0.0.1:1242/v1/chat/completions")

    def test_chain_defaults_to_declaration_order(self):
        config = _config(TWO_GEMINIS.replace('chain = ["gemini-b", "gemini-a", "local"]', ""))

        self.assertEqual(config.chain, ("gemini-a", "gemini-b", "local"))

    def test_invalid_configs_are_rejected_with_the_reason(self):
        cases = {
            "unknown type": ('[providers.x]\ntype = "anthropic"\nmodel = "m"\n', "type"),
            "inline key": (
                '[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key = "AIza-secret"\n',
                "api_key_env",
            ),
            "remote without key source": ('[providers.x]\ntype = "openrouter"\nmodel = "m"\n',
                                          "api_key"),
            "openai without url": ('[providers.x]\ntype = "openai"\nmodel = "m"\n', "url"),
            "missing model": ('[providers.x]\ntype = "gemini"\napi_key_env = "K"\n', "model"),
            "file without var": (
                '[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_file = "/k.env"\n',
                "api_key_var",
            ),
            "unknown field": (
                '[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "K"\nbudget = 3\n',
                "budget",
            ),
            "unknown chain name": (
                'chain = ["y"]\n[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "K"\n',
                "y",
            ),
            "no providers": ("chain = []\n", "provider"),
            "string boolean": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                               'serial = "false"\n', "serial"),
            "string number": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                              'idle_seconds = "30"\n', "idle_seconds"),
            "bool budget": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                            'budget_s = true\n', "budget_s"),
            "infinite budget": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                                'budget_s = inf\n', "budget_s"),
            "metrics as a string": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\n'
                                    'model = "m"\nidle_metrics = "http://h/metrics"\n',
                                    "idle_metrics"),
            "https metrics": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                              'idle_metrics = ["https://h/metrics"]\n', "idle_metrics"),
            "non-string url": ('[providers.x]\ntype = "openai"\nurl = 123\nmodel = "m"\n', "url"),
            "bad url port": ('[providers.x]\ntype = "openai"\nurl = "http://h:abc/v1"\n'
                             'model = "m"\n', "url"),
            "non-string key env": ('[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = 1\n',
                                   "api_key_env"),
            "key pasted as env name": (
                '[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "sk-or-v1-abc"\n',
                "api_key_env",
            ),
            "var without file": ('[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "K"\n'
                                 'api_key_var = "V"\n', "api_key_file"),
            "chain as a string": ('chain = "x"\n[providers.x]\ntype = "gemini"\nmodel = "m"\n'
                                  'api_key_env = "K"\n', "chain"),
            "empty chain": ('chain = []\n[providers.x]\ntype = "gemini"\nmodel = "m"\n'
                            'api_key_env = "K"\n', "chain"),
            "duplicate chain name": ('chain = ["x", "x"]\n[providers.x]\ntype = "gemini"\n'
                                     'model = "m"\napi_key_env = "K"\n', "x"),
            "unknown top-level key": ('chian = ["x"]\n[providers.x]\ntype = "gemini"\n'
                                      'model = "m"\napi_key_env = "K"\n', "chian"),
            "gate on a remote type": (
                '[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "K"\nserial = true\n',
                "serial",
            ),
        }
        for label, (text, needle) in cases.items():
            with self.subTest(label):
                with self.assertRaises(router.ConfigError) as caught:
                    _config(text)
                self.assertIn(needle, str(caught.exception))

    def test_secret_values_never_appear_in_errors(self):
        with self.assertRaises(router.ConfigError) as caught:
            _config('[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key = "AIza-secret"\n')

        self.assertNotIn("AIza-secret", str(caught.exception))
        with self.assertRaises(router.ConfigError) as caught:
            _config('[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "sk-or-v1-abc"\n')
        self.assertNotIn("sk-or-v1-abc", str(caught.exception))

    def test_the_shipped_example_is_a_valid_config(self):
        config = router.load_config(str(MODULE_PATH.with_name("config.example.toml")))

        self.assertEqual(config.chain, ("gemini-a", "gemini-b", "openrouter", "cursor", "local"))

    def test_removed_environment_variables_are_named(self):
        stale = router.stale_environment({"OBSERVER_LOCAL_URL": "x", "OBSERVER_ROUTER_PORT": "1",
                                          "OBSERVER_CURSOR_MODEL": "y", "HOME": "/h"})

        self.assertEqual(stale, ["OBSERVER_CURSOR_MODEL", "OBSERVER_LOCAL_URL"])

    def test_chain_override_selects_and_orders_instances(self):
        config = _config(TWO_GEMINIS)

        self.assertEqual(router.chain_override(config, "local, gemini-a"), ("local", "gemini-a"))
        self.assertEqual(router.chain_override(config, ""), config.chain)
        with self.assertRaises(router.ConfigError):
            router.chain_override(config, "gemini-a,nope")


class PromptCompactionTests(unittest.TestCase):
    def setUp(self):
        self.total_limit = router.MAX_PROMPT_CHARS
        self.field_limit = router.MAX_FIELD_CHARS
        router.MAX_PROMPT_CHARS = 1_000
        router.MAX_FIELD_CHARS = 400

    def tearDown(self):
        router.MAX_PROMPT_CHARS = self.total_limit
        router.MAX_FIELD_CHARS = self.field_limit

    def test_small_messages_are_unchanged(self):
        messages = [
            {"role": "system", "content": "Observe accurately."},
            {"role": "user", "content": "Summarize this session."},
        ]

        compacted, stats = router.compact_messages(messages)

        self.assertEqual(compacted, messages)
        self.assertFalse(stats["changed"])

    def test_image_parts_and_base64_blobs_are_removed(self):
        blob = "A" * 20_000
        messages = [{
            "role": "tool",
            "content": [
                {"type": "text", "text": f"before {blob} after"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }]

        compacted, stats = router.compact_messages(messages)
        content = compacted[0]["content"]

        self.assertIn("before", content)
        self.assertIn("after", content)
        self.assertIn("binary payload omitted", content)
        self.assertIn("image omitted", content)
        self.assertNotIn(blob, content)
        self.assertGreater(stats["removed_blobs"], 0)
        self.assertEqual(stats["removed_image_parts"], 1)

    def test_oversized_history_keeps_system_and_newest_context(self):
        messages = [
            {"role": "system", "content": "S" * 300},
            {"role": "user", "content": "old-" + "x" * 500},
            {"role": "assistant", "content": "middle-" + "y" * 500},
            {"role": "user", "content": "newest-" + "z" * 500},
        ]

        compacted, stats = router.compact_messages(messages)
        combined = "".join(str(message["content"]) for message in compacted)

        self.assertLessEqual(router._message_chars(compacted), router.MAX_PROMPT_CHARS)
        self.assertEqual(compacted[0]["role"], "system")
        self.assertIn("newest-", combined)
        self.assertNotIn("old-", combined)
        self.assertTrue(stats["changed"])
        self.assertGreater(stats["dropped_messages"], 0)


def _use(tables: dict, chain=None) -> None:
    """Make these provider tables the router's active configuration."""
    config = router.parse_config({"providers": tables})
    router.configure(config, tuple(chain) if chain else None)


REMOTE = {"type": "openai", "url": "http://127.0.0.1:9/v1/chat/completions", "model": "r"}
LANE = {**REMOTE, "model": "lane", "serial": True, "ac_only": True}


class ActiveConfigTestCase(unittest.TestCase):
    """Restores the active configuration after each test."""

    def setUp(self):
        self.saved_config = (router.PROVIDERS, router.CHAIN)

    def tearDown(self):
        router.PROVIDERS, router.CHAIN = self.saved_config


class PowerGateTests(ActiveConfigTestCase):
    """A GPU lane marked ac_only is left out while on battery; other providers still run.

    power_source() is exercised through its cache so the tests never shell out to pmset.
    """

    def setUp(self):
        super().setUp()
        _use({"remote": REMOTE, "local": LANE})

    def tearDown(self):
        super().tearDown()
        router._power_cache["checked_at"] = 0.0
        router._power_cache["source"] = "unknown"

    def _pin_power(self, source: str) -> None:
        router._power_cache["checked_at"] = router.time.time()
        router._power_cache["source"] = source

    def test_ac_only_provider_runs_on_ac_power(self):
        self._pin_power("AC Power")

        allowed, skipped = router.allowed_tiers()

        self.assertEqual(allowed, ["remote", "local"])
        self.assertEqual(skipped, {})

    def test_ac_only_provider_is_skipped_on_battery_with_a_reason(self):
        self._pin_power("Battery Power")

        allowed, skipped = router.allowed_tiers()

        self.assertEqual(allowed, ["remote"])
        self.assertIn("battery", skipped["local"])

    def test_unreadable_power_state_keeps_the_provider(self):
        self._pin_power("unknown")

        allowed, _ = router.allowed_tiers()

        self.assertIn("local", allowed)

    def test_a_provider_without_ac_only_runs_on_battery(self):
        _use({"remote": REMOTE, "local": {**LANE, "ac_only": False}})
        self._pin_power("Battery Power")

        allowed, skipped = router.allowed_tiers()

        self.assertEqual(allowed, ["remote", "local"])
        self.assertEqual(skipped, {})

    def test_snapshot_reports_the_gate(self):
        self._pin_power("Battery Power")

        snapshot = router.power_snapshot()

        self.assertEqual(snapshot["source"], "Battery Power")
        self.assertEqual(snapshot["ac_only"], ["local"])
        self.assertFalse(snapshot["ac_only_allowed"])


class RequestBuildingTests(unittest.TestCase):
    """Each provider instance sends its own model, key, and type-specific fields."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(router.os.environ)

    def tearDown(self):
        router.os.environ.clear()
        router.os.environ.update(self.env)
        self.tmp.cleanup()

    def _file(self, name: str, text: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_text(text)
        return str(path)

    def _provider(self, **fields) -> Any:
        table = {"type": "openai", "url": "http://127.0.0.1:9/v1/chat/completions", "model": "m"}
        table.update(fields)
        return router.parse_config({"providers": {"p": table}}).providers["p"]

    def test_key_from_the_named_environment_variable(self):
        router.os.environ["TEST_KEY_A"] = " abc "
        provider = self._provider(type="gemini", api_key_env="TEST_KEY_A")

        self.assertEqual(router.headers_for(provider)["Authorization"], "Bearer abc")

    def test_key_from_a_dotenv_file(self):
        path = self._file("bridge.cfg", '# comment\nPORT=8765\nexport BRIDGE_KEY="abc123"\n')
        provider = self._provider(api_key_file=path, api_key_var="BRIDGE_KEY")

        self.assertEqual(router.headers_for(provider)["Authorization"], "Bearer abc123")

    def test_key_from_a_json_settings_file(self):
        path = self._file("settings.json", json.dumps({"CLAUDE_MEM_GEMINI_API_KEY": "g-key"}))
        provider = self._provider(
            type="gemini", api_key_file=path, api_key_var="CLAUDE_MEM_GEMINI_API_KEY"
        )

        self.assertEqual(router.headers_for(provider)["Authorization"], "Bearer g-key")

    def test_environment_wins_over_the_file(self):
        router.os.environ["TEST_KEY_A"] = "from-env"
        path = self._file("k.env", "K=from-file\n")
        provider = self._provider(api_key_env="TEST_KEY_A", api_key_file=path, api_key_var="K")

        self.assertEqual(router.headers_for(provider)["Authorization"], "Bearer from-env")

    def test_a_missing_key_fails_the_provider_without_naming_a_value(self):
        router.os.environ.pop("TEST_KEY_MISSING", None)
        provider = self._provider(
            type="gemini", api_key_env="TEST_KEY_MISSING",
            api_key_file=str(Path(self.tmp.name) / "absent.env"), api_key_var="K",
        )

        with self.assertRaises(router.TierFailure) as caught:
            router.headers_for(provider)
        self.assertIn("p:", str(caught.exception))

    def test_a_key_with_a_line_break_fails_without_echoing_it(self):
        router.os.environ["TEST_KEY_A"] = "sk-SECRET\nsecond-line"
        provider = self._provider(type="gemini", api_key_env="TEST_KEY_A")

        with self.assertRaises(router.TierFailure) as caught:
            router.headers_for(provider)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_an_unreadable_key_file_is_named_as_the_cause(self):
        provider = self._provider(
            type="gemini", api_key_file=str(Path(self.tmp.name) / "absent.env"), api_key_var="K"
        )

        with self.assertRaises(router.TierFailure) as caught:
            router.headers_for(provider)
        self.assertIn("cannot read", str(caught.exception))

    def test_dotenv_last_assignment_wins_and_inline_comments_are_dropped(self):
        path = self._file("k.env", "K=old\nK=new # rotated\nQ='quoted # kept'\nR=\"r\" # note\n")

        self.assertEqual(router.api_key(self._provider(api_key_file=path, api_key_var="K")), "new")
        self.assertEqual(
            router.api_key(self._provider(api_key_file=path, api_key_var="Q")), "quoted # kept"
        )
        self.assertEqual(router.api_key(self._provider(api_key_file=path, api_key_var="R")), "r")

    def test_keyless_openai_provider_sends_no_authorization(self):
        self.assertNotIn("Authorization", router.headers_for(self._provider()))

    def test_openrouter_sends_attribution_headers(self):
        router.os.environ["TEST_KEY_A"] = "k"
        provider = self._provider(
            type="openrouter", api_key_env="TEST_KEY_A", site_url="https://x.test", app_name="me"
        )

        headers = router.headers_for(provider)

        self.assertEqual(headers["HTTP-Referer"], "https://x.test")
        self.assertEqual(headers["X-Title"], "me")

    def test_payload_uses_the_instance_model_and_passes_sampling_fields(self):
        body = {"messages": [{"role": "user", "content": "x"}], "max_tokens": 5, "seed": 1}

        payload = router.payload_for(self._provider(model="composer-2.5"), body, None)

        self.assertEqual(payload["model"], "composer-2.5")
        self.assertEqual(payload["max_tokens"], 5)
        self.assertNotIn("seed", payload)
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_reasoning_effort_only_when_configured(self):
        payload = router.payload_for(self._provider(reasoning_effort="none"), {"messages": []}, None)

        self.assertEqual(payload["reasoning_effort"], "none")

    def test_openrouter_reasoning_modes(self):
        router.os.environ["TEST_KEY_A"] = "k"
        provider = self._provider(type="openrouter", api_key_env="TEST_KEY_A")

        disabled = router.payload_for(provider, {"messages": []}, "disabled")
        excluded = router.payload_for(provider, {"messages": []}, "exclude")

        self.assertEqual(disabled["reasoning"], {"enabled": False})
        self.assertEqual(excluded["reasoning"], {"exclude": True})

    def test_only_serial_providers_share_a_lane_lock(self):
        serial = self._provider(serial=True)
        same_lane = self._provider(serial=True)
        free = self._provider()

        self.assertIs(router.lane_guard(serial), router.lane_guard(same_lane))
        self.assertIsNot(router.lane_guard(free), router.lane_guard(serial))


def _gemini_429(quota_ids, retry_delay="34s"):
    """A Gemini 429 in the shape the OpenAI-compatible endpoint returned on 2026-09-24."""
    return json.dumps([{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
             "quotaId": quota_id} for quota_id in quota_ids
        ]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
    ]}}], indent=2)


def _http_error(code, body, headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return HTTPError("https://example.invalid", code, "error", message, BytesIO(body.encode()))


class QuotaResetTests(ActiveConfigTestCase):
    """A 429 that states its reset blocks the tier until then instead of re-probing it."""

    # 2026-09-24 07:28:25 UTC = 00:28:25 PDT
    NOW = 1790234905.0

    def setUp(self):
        super().setUp()
        _use({"gemini": {**REMOTE, "type": "gemini", "api_key_env": "K"}})
        with router._breaker_lock:
            self.saved = (dict(router._failures), dict(router._skip_until),
                          dict(router._skip_reason))

    def tearDown(self):
        super().tearDown()
        with router._breaker_lock:
            for target, saved in zip(
                (router._failures, router._skip_until, router._skip_reason), self.saved
            ):
                target.clear()
                target.update(saved)

    def test_gemini_daily_quota_blocks_until_pacific_midnight(self):
        error = _http_error(429, _gemini_429([
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        ]))

        retry_at, quota = router.quota_reset("gemini", error, now=self.NOW)

        self.assertEqual(quota, "GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        self.assertEqual(retry_at, 1790319600.0)  # 2026-09-25 00:00 PDT

    def test_gemini_per_minute_quota_uses_retry_delay(self):
        error = _http_error(429, _gemini_429(
            ["GenerateRequestsPerMinutePerProjectPerModel-FreeTier"], retry_delay="34.94s"
        ))

        retry_at, quota = router.quota_reset("gemini", error, now=self.NOW)

        self.assertAlmostEqual(retry_at, self.NOW + 34.94)
        self.assertEqual(quota, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")

    def test_openrouter_reset_header_is_epoch_milliseconds(self):
        body = json.dumps({"error": {"code": 429, "metadata": {
            "headers": {"X-RateLimit-Reset": "1790294400000"},
            "limit_source": "openrouter_free_tier_daily",
        }}})
        error = _http_error(429, body, {"X-RateLimit-Reset": "1790294400000"})

        retry_at, quota = router.quota_reset("openrouter", error, now=self.NOW)

        self.assertEqual(retry_at, 1790294400.0)  # 2026-09-25 00:00 UTC
        self.assertEqual(quota, "openrouter_free_tier_daily")

    def test_openrouter_reset_is_read_from_the_body_when_headers_lack_it(self):
        body = json.dumps(
            {"error": {"metadata": {"headers": {"X-RateLimit-Reset": "1790294400000"}}}}
        )

        retry_at, _ = router.quota_reset("openrouter", _http_error(429, body), now=self.NOW)

        self.assertEqual(retry_at, 1790294400.0)

    def test_unreadable_or_implausible_resets_fall_back_to_the_breaker(self):
        thirty_days_out = str(int(self.NOW * 1000) + 30 * 86_400_000)
        cases = [
            ("gemini", _http_error(429, "not json")),
            ("gemini", _http_error(500, _gemini_429(["GenerateRequestsPerDayPerProjectPerModel"]))),
            ("openrouter", _http_error(429, "{}", {"X-RateLimit-Reset": "garbage"})),
            ("openrouter", _http_error(429, "{}", {"X-RateLimit-Reset": "1000"})),  # past
            ("openrouter", _http_error(429, "{}", {"X-RateLimit-Reset": thirty_days_out})),
            ("cursor", _http_error(429, _gemini_429(["GenerateRequestsPerDayPerProjectPerModel"]))),
        ]
        for tier, error in cases:
            with self.subTest(tier=tier, code=error.code):
                self.assertIsNone(router.quota_reset(tier, error, now=self.NOW))

    def test_error_body_survives_repeated_reads(self):
        error = _http_error(400, "Reasoning is mandatory for this endpoint")

        self.assertTrue(router._is_mandatory_reasoning_error(error))
        self.assertIn("mandatory", router._snippet(error))

    def test_quota_failure_blocks_the_tier_with_its_reason(self):
        until = router.time.time() + 3600
        router.record_failure("gemini", router.TierFailure("x", until, "PerDayQuota"))

        reason = router.tier_skip_reason("gemini")

        self.assertIn("quota PerDayQuota until", reason)
        self.assertFalse(router.tier_available("gemini"))
        self.assertIn("quota PerDayQuota", router.breaker_snapshot()["gemini"]["reason"])

    def test_breaker_cannot_shorten_a_quota_block(self):
        until = router.time.time() + 3600
        router.record_failure("gemini", router.TierFailure("x", until, "PerDayQuota"))
        for _ in range(router.BREAK_AFTER):
            router.record_failure("gemini", router.TierFailure("gemini: HTTP 503"))

        self.assertEqual(router._skip_until["gemini"], until)
        self.assertIn("quota", router.tier_skip_reason("gemini"))

    def test_success_clears_the_block(self):
        router.record_failure("gemini", router.TierFailure("x", router.time.time() + 60, "q"))

        router.tier_ok("gemini")

        self.assertIsNone(router.tier_skip_reason("gemini"))


SPLASH_METRICS = """# HELP splash_requests_submitted_total Requests submitted.
splash_frontend_active 0
splash_requests_submitted_total {submitted}
splash_requests_completed_total {completed}
splash_requests_cancelled_total 10
splash_requests_failed_total 0
splash_scheduler_decoding 1
"""


class IdleGateTests(ActiveConfigTestCase):
    """A provider with idle_metrics waits until every watched lane has been idle long enough."""

    URL = "http://127.0.0.1:1240/metrics"

    def setUp(self):
        super().setUp()
        _use({"remote": REMOTE,
              "local": {**LANE, "ac_only": False, "idle_metrics": [self.URL], "idle_seconds": 30}})
        self.local = router.PROVIDERS["local"]
        router._lane_activity.clear()

    def tearDown(self):
        super().tearDown()
        router._lane_activity.clear()

    def _sample(self, submitted, completed, now):
        text = SPLASH_METRICS.format(submitted=submitted, completed=completed)
        router.record_lane_sample(self.URL, router.parse_lane_metrics(text), now)

    def test_in_flight_is_submitted_minus_finished(self):
        text = SPLASH_METRICS.format(submitted=1011, completed=1000)

        self.assertEqual(router.parse_lane_metrics(text), (1011, 1))

    def test_missing_counters_are_unreadable(self):
        self.assertIsNone(router.parse_lane_metrics("splash_requests_submitted_total 3\n"))

    def test_a_lane_never_seen_active_is_idle(self):
        self._sample(1010, 1000, now=1_000.0)

        self.assertEqual(router.busy_lanes(self.local, now=1_000.0), [])

    def test_an_in_flight_request_makes_the_lane_busy_for_the_grace_period(self):
        self._sample(1011, 1000, now=1_000.0)

        self.assertEqual(router.busy_lanes(self.local, now=1_029.0), [self.URL])
        self.assertEqual(router.busy_lanes(self.local, now=1_030.0), [])

    def test_a_request_finished_between_samples_still_counts_as_activity(self):
        self._sample(1010, 1000, now=1_000.0)
        self._sample(1012, 1002, now=1_002.0)  # two requests came and went

        self.assertEqual(router.busy_lanes(self.local, now=1_020.0), [self.URL])

    def test_an_unreadable_lane_ages_into_idle(self):
        self._sample(1011, 1000, now=1_000.0)
        router.record_lane_sample(self.URL, None, now=1_010.0)

        self.assertEqual(router.busy_lanes(self.local, now=1_031.0), [])

    def test_busy_lane_keeps_the_provider_out_with_a_reason(self):
        self._sample(1011, 1000, now=router.time.time())

        allowed, skipped = router.allowed_tiers()

        self.assertEqual(allowed, ["remote"])
        self.assertIn("lanes busy (127.0.0.1:1240", skipped["local"])
        self.assertFalse(router.idle_snapshot()["local"]["allowed"])

    def test_the_sampler_watches_only_lanes_of_providers_in_the_chain(self):
        self.assertEqual(router.watched_lanes(), (self.URL,))

        _use({"remote": REMOTE, "local": {**LANE, "idle_metrics": [self.URL]}}, chain=["remote"])

        self.assertEqual(router.watched_lanes(), ())

    def test_gate_is_off_without_watched_lanes(self):
        _use({"local": LANE})

        self.assertIsNone(router.busy_reason(router.PROVIDERS["local"]))


class FakeUpstream:
    """An OpenAI-compatible upstream that answers every POST with one scripted reply."""

    def __init__(self, status=200, body=None, stream_pieces=None):
        self.status, self.body, self.stream_pieces = status, body, stream_pieces
        self.requests: list[tuple[dict, dict]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                upstream.requests.append((dict(self.headers), json.loads(self.rfile.read(length))))
                if upstream.stream_pieces is not None and upstream.status == 200:
                    frames = "".join(
                        f"data: {json.dumps({'model': 'fake', 'choices': [{'delta': delta}]})}\n\n"
                        for delta in upstream.stream_pieces
                    ) + "data: [DONE]\n\n"
                    data = frames.encode()
                    content_type = "text/event-stream"
                else:
                    data = json.dumps(upstream.body).encode()
                    content_type = "application/json"
                self.send_response(upstream.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _completion(text):
    return {"model": "fake", "choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ChainTests(ActiveConfigTestCase):
    """Requests through the router's HTTP surface fail over between named instances."""

    def setUp(self):
        super().setUp()
        self.env = dict(router.os.environ)
        router.os.environ.update({"TEST_GEMINI_A": "key-a", "TEST_GEMINI_B": "key-b"})
        with router._breaker_lock:
            self.saved_breaker = (dict(router._failures), dict(router._skip_until),
                                  dict(router._skip_reason))
        self.upstreams: list[FakeUpstream] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), router.RouterHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        super().tearDown()
        self.server.shutdown()
        self.server.server_close()
        for upstream in self.upstreams:
            upstream.close()
        router.os.environ.clear()
        router.os.environ.update(self.env)
        with router._breaker_lock:
            for target, saved in zip(
                (router._failures, router._skip_until, router._skip_reason), self.saved_breaker
            ):
                target.clear()
                target.update(saved)

    def _upstream(self, **kwargs) -> FakeUpstream:
        upstream = FakeUpstream(**kwargs)
        self.upstreams.append(upstream)
        return upstream

    def _two_geminis(self, first: FakeUpstream, second: FakeUpstream) -> None:
        _use({
            "gemini-a": {"type": "gemini", "url": first.url, "model": "model-a",
                         "api_key_env": "TEST_GEMINI_A"},
            "gemini-b": {"type": "gemini", "url": second.url, "model": "model-b",
                         "api_key_env": "TEST_GEMINI_B"},
        })

    def _post(self, body: dict) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{self.base}/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def _get_health(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/health", timeout=10) as response:
            return json.load(response)

    def test_a_quota_blocked_instance_fails_over_to_a_second_instance_of_the_same_type(self):
        first = self._upstream(status=429, body=json.loads(_gemini_429(
            ["GenerateRequestsPerDayPerProjectPerModel-FreeTier"]))[0])
        second = self._upstream(body=_completion("from b"))
        self._two_geminis(first, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "from b")
        self.assertEqual(first.requests[0][0]["Authorization"], "Bearer key-a")
        self.assertEqual(first.requests[0][1]["model"], "model-a")
        self.assertEqual(second.requests[0][0]["Authorization"], "Bearer key-b")
        self.assertEqual(second.requests[0][1]["model"], "model-b")
        breaker = self._get_health()["breaker"]
        self.assertIn("quota GenerateRequestsPerDay", breaker["gemini-a"]["reason"])
        self.assertEqual(breaker["gemini-b"]["reason"], "")

    def test_a_blocked_instance_is_skipped_on_the_next_request(self):
        first = self._upstream(status=429, body=json.loads(_gemini_429(
            ["GenerateRequestsPerDayPerProjectPerModel-FreeTier"]))[0])
        second = self._upstream(body=_completion("from b"))
        self._two_geminis(first, second)

        self._post({"messages": [{"role": "user", "content": "one"}]})
        self._post({"messages": [{"role": "user", "content": "two"}]})

        self.assertEqual(len(first.requests), 1)
        self.assertEqual(len(second.requests), 2)

    def test_an_empty_stream_fails_over_and_the_next_instance_streams_content(self):
        first = self._upstream(stream_pieces=[{"reasoning": "thinking"}, {"content": ""}])
        second = self._upstream(stream_pieces=[{"content": "hel"}, {"content": "lo"}])
        self._two_geminis(first, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        frames = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith("data: {")]
        self.assertEqual("".join(f["choices"][0]["delta"]["content"] for f in frames), "hello")

    def test_all_instances_failing_names_each_one(self):
        first = self._upstream(status=503, body={"error": "down"})
        second = self._upstream(body=_completion(""))
        self._two_geminis(first, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 502)
        attempted = json.loads(raw)["attempted"]
        self.assertTrue(attempted[0].startswith("gemini-a("), attempted)
        self.assertTrue(attempted[1].startswith("gemini-b("), attempted)
        self.assertIn("empty content", attempted[1])

    def test_health_lists_each_instance_in_chain_order(self):
        self._two_geminis(self._upstream(body={}), self._upstream(body={}))

        health = self._get_health()

        self.assertEqual(health["chain"], ["gemini-a", "gemini-b"])
        self.assertEqual(health["providers"]["gemini-b"],
                         {"type": "gemini", "model": "model-b", "budget_s": 18.0})


if __name__ == "__main__":
    unittest.main()
