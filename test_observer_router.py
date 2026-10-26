#!/usr/bin/env python3
"""Contract tests for observer-router: prompt compaction, the power and idle gates, the cursor
tier, and quota-reset blocks."""

import contextlib
import http.client
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
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


def _fresh_router(env: dict) -> Any:
    """Load a separate copy of the router with these environment variables set at import."""
    saved = dict(router.os.environ)
    router.os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("observer_router_fresh", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop("observer_router_fresh", None)
        router.os.environ.clear()
        router.os.environ.update(saved)


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


POOL = """
chain = ["pool", "local"]

[providers.a]
type = "openai"
url = "http://127.0.0.1:9/a"
model = "m"

[providers.b]
type = "openai"
url = "http://127.0.0.1:9/b"
model = "m"

[providers.c]
type = "openai"
url = "http://127.0.0.1:9/c"
model = "m"

[providers.local]
type = "openai"
url = "http://127.0.0.1:9/local"
model = "m"

[groups.pool]
members = ["a", "b", "c"]
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
            "template kwargs not a table": (
                '[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                'chat_template_kwargs = "enable_thinking=false"\n', "chat_template_kwargs"),
            "template kwargs on a remote type": (
                '[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "K"\n'
                'chat_template_kwargs = {enable_thinking = false}\n', "chat_template_kwargs"),
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

        self.assertEqual(config.chain, ("gemini", "openrouter", "cursor", "local"))
        self.assertEqual(config.groups["gemini"], ("gemini-a", "gemini-b"))

    def test_a_group_rotates_its_members_across_requests(self):
        config = _config(POOL)
        router.configure(config)
        try:
            orders = [router.expand_chain() for _ in range(4)]
        finally:
            router.configure(router.Config({}, ()))

        self.assertEqual(config.chain, ("pool", "local"))
        self.assertEqual(config.groups["pool"], ("a", "b", "c"))
        self.assertEqual(orders, [
            ["a", "b", "c", "local"],
            ["b", "c", "a", "local"],
            ["c", "a", "b", "local"],
            ["a", "b", "c", "local"],
        ])

    def test_invalid_groups_are_rejected_with_the_reason(self):
        providers = POOL.split("[groups.pool]")[0].replace('chain = ["pool", "local"]', "")
        cases = {
            "unknown member": ('[groups.g]\nmembers = ["a", "zz"]\n', "zz"),
            "empty members": ('[groups.g]\nmembers = []\n', "members"),
            "repeated member": ('[groups.g]\nmembers = ["a", "a"]\n', "repeats"),
            "unknown field": ('[groups.g]\nmembers = ["a"]\nstrategy = "random"\n', "strategy"),
            "groups not a table": ('groups = []\n', "groups must be"),
            "name taken by a provider": ('[groups.a]\nmembers = ["b"]\n', "groups.a"),
            "group inside a group": ('[groups.g]\nmembers = ["a"]\n[groups.h]\nmembers = ["g"]\n',
                                     "groups.h"),
            "member also in the chain": ('chain = ["g", "a"]\n[groups.g]\nmembers = ["a", "b"]\n',
                                         "more than once"),
        }
        for label, (text, needle) in cases.items():
            with self.subTest(label):
                chain = "" if text.startswith("chain") else 'chain = ["g"]\n'
                with self.assertRaises(router.ConfigError) as caught:
                    _config(chain + text + providers)
                self.assertIn(needle, str(caught.exception))

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


class EnvironmentTests(unittest.TestCase):
    """Router-wide OBSERVER_* settings are checked at start-up, like the config file."""

    def _start(self, env: dict) -> subprocess.CompletedProcess:
        with socket.socket() as probe:  # a free port, released before the router binds it
            probe.bind(("127.0.0.1", 0))
            port = str(probe.getsockname()[1])
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[providers.x]\ntype = "openai"\nurl = "http://127.0.0.1:9/v1"\n'
                              'model = "m"\n')
            clean = {k: v for k, v in router.os.environ.items() if not k.startswith("OBSERVER_")}
            try:
                return subprocess.run(
                    [sys.executable, str(MODULE_PATH)], capture_output=True, text=True, timeout=5,
                    env={**clean, "OBSERVER_ROUTER_CONFIG": str(config),
                         "OBSERVER_ROUTER_PORT": port, **env},
                )
            except subprocess.TimeoutExpired:
                self.fail(f"the router started despite {env}")

    def test_an_invalid_setting_stops_the_router_and_names_it(self):
        # Rule: a value that is not a number, or is out of range, is an error at start-up (exit
        # 2, the variable named), never a silent fallback to the default.
        cases = {
            "not a number": {"OBSERVER_BREAK_AFTER": "three"},
            "zero strikes": {"OBSERVER_GROUP_STRIKES": "0"},
            "negative seconds": {"OBSERVER_BREAK_SECONDS": "-1"},
            "infinite seconds": {"OBSERVER_IDLE_SAMPLE_SECONDS": "inf"},
            "port out of range": {"OBSERVER_ROUTER_PORT": "70000"},
            "zero prompt budget": {"OBSERVER_MAX_PROMPT_CHARS": "0"},
            "integer too big for a float": {"OBSERVER_MAX_PROMPT_CHARS": "9" * 400},
            "blob size past the regex limit": {"OBSERVER_BASE64_BLOB_CHARS": "99999999999"},
            "sample interval too long to sleep": {"OBSERVER_IDLE_SAMPLE_SECONDS": "1e30"},
            "allowed host that is not a name": {"OBSERVER_ROUTER_ALLOWED_HOSTS": "ok.lan,bad host"},
        }
        for label, env in cases.items():
            with self.subTest(label):
                result = self._start(env)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(next(iter(env)), result.stdout)


def _use(tables: dict, chain=None) -> None:
    """Make these provider tables the router's active configuration."""
    config = router.parse_config({"providers": tables})
    router.configure(config, tuple(chain) if chain else None)


REMOTE = {"type": "openai", "url": "http://127.0.0.1:9/v1/chat/completions", "model": "r"}
LANE = {**REMOTE, "model": "lane", "serial": True, "ac_only": True}


class ActiveConfigTestCase(unittest.TestCase):
    """Restores the active configuration after each test."""

    def setUp(self):
        self.saved_config = (router.PROVIDERS, router.CHAIN, router.GROUPS)

    def tearDown(self):
        router.PROVIDERS, router.CHAIN, router.GROUPS = self.saved_config


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

    def test_a_timeout_shortened_by_the_request_deadline_is_not_an_overload(self):
        slow = FakeUpstream(body={"choices": [{"message": {"content": "x"}}]}, delay=2.0)
        try:
            provider = self._provider(url=slow.url, budget_s=4)

            with self.assertRaises(router.TierFailure) as caught:
                router.nonstream(provider, {"messages": []}, deadline=router.time.time() + 1)
        finally:
            slow.close()
        self.assertFalse(caught.exception.overloaded)

    def test_a_full_budget_timeout_is_an_overload(self):
        slow = FakeUpstream(body={"choices": [{"message": {"content": "x"}}]}, delay=2.0)
        try:
            provider = self._provider(url=slow.url, budget_s=1)

            with self.assertRaises(router.TierFailure) as caught:
                router.nonstream(provider, {"messages": []}, deadline=router.time.time() + 60)
        finally:
            slow.close()
        self.assertTrue(caught.exception.overloaded)

    def test_an_openrouter_retry_that_times_out_keeps_the_overload_flag(self):
        router.os.environ["TEST_KEY_A"] = "k"
        upstream = FakeUpstream(script=[
            (400, {"error": {"message": "Reasoning is mandatory for this endpoint"}}, 0.0),
            (200, {"choices": [{"message": {"content": "x"}}]}, 2.0),
        ])
        try:
            provider = self._provider(type="openrouter", url=upstream.url, budget_s=1,
                                      api_key_env="TEST_KEY_A")

            with self.assertRaises(router.TierFailure) as caught:
                router.nonstream(provider, {"messages": []}, deadline=router.time.time() + 60)
        finally:
            upstream.close()
        self.assertTrue(caught.exception.overloaded)

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

    def test_chat_template_kwargs_are_sent_as_configured(self):
        provider = self._provider(chat_template_kwargs={"enable_thinking": False})

        payload = router.payload_for(provider, {"messages": []}, None)

        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("chat_template_kwargs", router.payload_for(self._provider(),
                                                                    {"messages": []}, None))

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


class RequestGuardTests(ActiveConfigTestCase):
    """A web page must not be able to drive the router: it takes neither a Host header that a
    rebound DNS name would carry, nor a POST that is not declared JSON."""

    def setUp(self):
        super().setUp()
        self.upstream = FakeUpstream(body=_completion("hi"))
        _use({"p": {"type": "openai", "url": self.upstream.url, "model": "m"}})
        self.saved_hosts = router.ALLOWED_HOSTS
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), router.RouterHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port

    def tearDown(self):
        super().tearDown()
        router.ALLOWED_HOSTS = self.saved_hosts
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()

    def _call(self, method, path, headers, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            self.last_connection_header = response.getheader("Connection")
            return response.status, response.read()
        finally:
            connection.close()

    CHAT = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()

    def test_a_host_header_naming_another_site_is_refused(self):
        # Rule: a DNS name that is not localhost or listed reaches the router only through a
        # rebound page; GET and POST are refused with 403 and no provider is called.
        for method, path, body in (("GET", "/health", None),
                                   ("POST", "/v1/chat/completions", self.CHAT)):
            with self.subTest(method):
                status, _ = self._call(method, path, {"Host": "evil.example:1244",
                                                      "Content-Type": "application/json"}, body)
                self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])

    def test_a_refusal_closes_the_connection(self):
        # Rule: a refused POST leaves its body unread, so the connection must not be reused,
        # or the body would be parsed as the next request.
        for label, headers in (("host", {"Host": "evil.example", "Content-Type": "application/json"}),
                               ("type", {"Content-Type": "text/plain"})):
            with self.subTest(label):
                self._call("POST", "/v1/chat/completions", headers, self.CHAT)
                self.assertEqual(self.last_connection_header, "close")

    def _raw(self, payload: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            connection.sendall(payload)
            connection.settimeout(1.0)
            received = b""
            try:
                while chunk := connection.recv(65536):
                    received += chunk
            except TimeoutError:
                pass
            return received

    def test_a_body_left_unread_is_never_parsed_as_the_next_request(self):
        # Rule: whatever refuses a POST, its body cannot become a second request on the same
        # connection. A page can post text to http://127.0.0.1:1244/anything (an IP literal passes
        # the Host check); if that body were parsed, it would carry the JSON the guard demands.
        inner = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                 b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                 % (len(self.CHAT), self.CHAT))
        for label, head in (("unknown path", b"POST /anything HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                              b"Content-Type: text/plain\r\n"),
                            ("wrong type", b"POST /v1/chat/completions HTTP/1.1\r\n"
                                           b"Host: 127.0.0.1\r\nContent-Type: text/plain\r\n")):
            with self.subTest(label):
                request = head + b"Content-Length: %d\r\n\r\n" % len(inner) + inner
                answered = self._raw(request).count(b"HTTP/1.")
                self.assertEqual(answered, 1)
        self.assertEqual(self.upstream.requests, [])

    def test_an_unusable_content_length_is_refused_and_closes_the_connection(self):
        # Rule: a Content-Length that is not a non-negative integer cannot say where the body
        # ends, so the request is refused rather than read as empty and the body left behind.
        for value in ("abc", "-5"):
            with self.subTest(value):
                reply = self._raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                  b"Content-Type: application/json\r\nContent-Length: " +
                                  value.encode() + b"\r\n\r\n{}")
                self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply[:80])
                self.assertIn(b"Connection: close", reply)

    def test_a_refusal_is_logged_with_what_was_refused(self):
        # Rule: a legitimate client that is turned away must be findable in the log: the status,
        # the request line, and the offending header, quoted and cut short.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._call("GET", "/health", {"Host": "evil.example"})
            self._call("POST", "/v1/chat/completions", {"Content-Type": "text/plain" + "x" * 300},
                       self.CHAT)
        lines = [line for line in out.getvalue().splitlines() if "refused" in line]
        self.assertEqual(len(lines), 2, out.getvalue())
        self.assertIn("refused 403 GET /health: Host='evil.example'", lines[0])
        self.assertIn("refused 415 POST /v1/chat/completions: Content-Type='text/plain", lines[1])
        self.assertNotIn("x" * 150, lines[1])

    def test_a_trailing_dot_does_not_change_the_host(self):
        # Rule: "localhost." is the same name as "localhost", so a browser that sends it is served;
        # a listed name is matched the same way, and another name stays refused with or without it.
        router.ALLOWED_HOSTS = ("router.lan",)
        for host, expected in (("localhost.:1244", 200), ("router.lan.", 200),
                               ("evil.example.", 403)):
            with self.subTest(host):
                status, _ = self._call("GET", "/health", {"Host": host})
                self.assertEqual(status, expected)

    def test_only_application_json_itself_is_taken(self):
        # Rule: a media type that merely starts like it, or lists another, is not JSON.
        for content_type in ("application/jsonx", "application/json,text/plain",
                             "text/plain, application/json", "application/x-ndjson",
                             "application/vnd.api+json"):
            with self.subTest(content_type):
                status, _ = self._call("POST", "/v1/chat/completions",
                                       {"Content-Type": content_type}, self.CHAT)
                self.assertEqual(status, 415)

    def test_loopback_and_literal_address_hosts_are_served(self):
        # Rule: an IP literal or localhost cannot be a rebound name, so any of them is served;
        # this keeps LAN access (a bound address) working.
        for host in ("localhost:1244", "127.0.0.1:1244", "[::1]:1244", "192.168.1.5:1244",
                     "127.0.0.1"):
            with self.subTest(host):
                status, _ = self._call("GET", "/health", {"Host": host})
                self.assertEqual(status, 200)

    def test_listed_host_names_are_read_trimmed_and_lower_cased(self):
        # Rule: OBSERVER_ROUTER_ALLOWED_HOSTS is a comma list; case and spaces do not matter.
        loaded = _fresh_router({"OBSERVER_ROUTER_ALLOWED_HOSTS": " Router.LAN , nas.local,"})
        self.assertEqual(loaded.ALLOWED_HOSTS, ("router.lan", "nas.local"))

    def test_a_listed_host_name_is_served(self):
        # Rule: OBSERVER_ROUTER_ALLOWED_HOSTS names the DNS names that may reach the router.
        router.ALLOWED_HOSTS = ("router.lan",)
        status, _ = self._call("GET", "/health", {"Host": "Router.LAN:1244"})
        self.assertEqual(status, 200)

    def test_a_post_that_is_not_declared_json_is_refused(self):
        # Rule: a cross-site form or text post carries another type, or none; only
        # application/json (with any parameters) is taken, so no provider is called otherwise.
        for label, headers in (("text", {"Content-Type": "text/plain"}),
                               ("form", {"Content-Type": "application/x-www-form-urlencoded"}),
                               ("none", {})):
            with self.subTest(label):
                status, _ = self._call("POST", "/v1/chat/completions", headers, self.CHAT)
                self.assertEqual(status, 415)
        self.assertEqual(self.upstream.requests, [])
        status, _ = self._call("POST", "/v1/chat/completions",
                               {"Content-Type": "Application/JSON; charset=utf-8"}, self.CHAT)
        self.assertEqual(status, 200)


class FakeUpstream:
    """An OpenAI-compatible upstream that answers every POST with one scripted reply."""

    def __init__(self, status=200, body=None, stream_pieces=None, delay=0.0, stall=0.0, script=(),
                 finish=None, pace=0.0):
        """delay holds back the whole reply; stall sends the headers, then holds back the body.
        script lists (status, body, delay) replies for the first requests, in order. finish is
        the stream's last frame before [DONE]: (finish_reason, usage). pace holds back each
        stream frame after the first, so a stream that keeps arriving can outlast a budget."""
        self.status, self.body, self.stream_pieces = status, body, stream_pieces
        self.finish, self.pace = finish, pace
        self.delay, self.stall, self.script = delay, stall, list(script)
        self.requests: list[tuple[dict, dict]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                upstream.requests.append((dict(self.headers), json.loads(self.rfile.read(length))))
                status, body, delay = (upstream.script.pop(0) if upstream.script
                                       else (upstream.status, upstream.body, upstream.delay))
                if upstream.stream_pieces is not None and status == 200:
                    frames = [
                        f"data: {json.dumps({'model': 'fake', 'choices': [{'delta': delta}]})}\n\n"
                        for delta in upstream.stream_pieces
                    ]
                    if upstream.finish:
                        reason, usage = upstream.finish
                        frames.append("data: " + json.dumps({"model": "fake", "usage": usage, "choices": [
                            {"delta": {}, "finish_reason": reason}]}) + "\n\n")
                    frames.append("data: [DONE]\n\n")
                    data = "".join(frames).encode()
                    content_type = "text/event-stream"
                else:
                    data = json.dumps(body).encode()
                    content_type = "application/json"
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(upstream.stall)
                    if upstream.pace and content_type == "text/event-stream":
                        for index, frame in enumerate(frames):
                            time.sleep(upstream.pace if index else 0)
                            self.wfile.write(frame.encode())
                            self.wfile.flush()
                    else:
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the router gave up on this upstream

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

    def test_a_reply_cut_off_at_the_token_limit_fails_over(self):
        # Rule: finish_reason=length is a truncated reply, not a result; the next tier answers.
        cut = self._upstream(body={"model": "fake", "choices": [
            {"message": {"content": "1 2 3 4 5 6"}, "finish_reason": "length"}]})
        second = self._upstream(body=_completion("from b"))
        self._two_geminis(cut, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "from b")
        self.assertEqual(len(second.requests), 1)

    def _buffered_then_fallback(self, first: FakeUpstream, fallback: FakeUpstream) -> None:
        _use({
            "lane": {"type": "openai", "url": first.url, "model": "lane", "buffer_stream": True},
            "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"},
        })

    @staticmethod
    def _streamed_text(raw: bytes) -> str:
        frames = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith("data: {")]
        return "".join(f["choices"][0]["delta"].get("content", "") for f in frames)

    def test_a_buffered_stream_cut_off_at_the_token_limit_fails_over(self):
        # Rule: a buffered provider's stream that ends finish_reason=length is never forwarded;
        # the next tier answers in full.
        cut = self._upstream(stream_pieces=[{"content": "loop "}, {"content": "loop "}],
                             finish=("length", {"prompt_tokens": 4270, "completion_tokens": 2048}))
        fallback = self._upstream(stream_pieces=[{"content": "fall"}, {"content": "back"}])
        self._buffered_then_fallback(cut, fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")
        # A cut-off depends on the prompt, not the provider: it does not count toward the breaker.
        self.assertEqual(self._get_health()["breaker"]["lane"]["failures"], 0)

    def test_a_buffered_stream_that_finishes_is_served_whole(self):
        # Rule: buffering changes when the text is sent, never what is sent.
        lane = self._upstream(stream_pieces=[{"content": "hel"}, {"content": "lo"}],
                              finish=("stop", {"prompt_tokens": 9, "completion_tokens": 2}))
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._buffered_then_fallback(lane, fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "hello")
        self.assertEqual(fallback.requests, [])

    def test_a_cut_off_reply_is_logged_with_its_size_and_both_ends(self):
        # Rule: one log line names the provider, prompt size, completion tokens and a short
        # head/tail of the text, so looping can be told from a genuinely long answer.
        text = "BEGIN" + "x" * 300 + "END"
        cut = self._upstream(stream_pieces=[{"content": text}],
                             finish=("length", {"prompt_tokens": 4270, "completion_tokens": 2048}))
        self._buffered_then_fallback(cut, self._upstream(stream_pieces=[{"content": "ok"}]))

        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        lines = [line for line in out.getvalue().splitlines() if "completion_tokens=" in line]
        self.assertEqual(len(lines), 1, out.getvalue())
        line = lines[0]
        self.assertIn("tier lane cut off at the token limit", line)
        self.assertIn("prompt_tokens=4270", line)
        self.assertIn("prompt_chars=2", line)
        self.assertIn("completion_tokens=2048", line)
        self.assertIn("head='BEGINxxx", line)
        self.assertIn("xxxEND'", line)
        self.assertNotIn("x" * 200, line)  # a preview, not the whole reply

    def test_an_unbuffered_stream_cut_off_is_still_logged_but_not_served_as_a_success(self):
        # Rule: without buffer_stream the text is already forwarded, so the cut cannot fail over;
        # it is still logged with its size and never recorded as a served reply. A cut-off says
        # nothing about the provider's health, so it does not count toward the breaker.
        cut = self._upstream(stream_pieces=[{"content": "partial"}],
                             finish=("length", {"prompt_tokens": 7, "completion_tokens": 3}))
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": cut.url, "model": "lane"},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        with contextlib.redirect_stdout(io.StringIO()) as out:
            status, raw = self._post({"messages": [{"role": "user", "content": "hi"}],
                                      "stream": True})

        self.assertEqual(self._streamed_text(raw), "partial")
        last = [json.loads(line[6:]) for line in raw.decode().splitlines()
                if line.startswith("data: {")][-1]
        self.assertEqual(last["choices"][0]["finish_reason"], "length")  # so the client knows
        self.assertIn("tier lane cut off at the token limit: prompt_tokens=7", out.getvalue())
        self.assertNotIn("served tier=lane", out.getvalue())
        self.assertEqual(self._get_health()["breaker"]["lane"]["failures"], 0)
        self.assertEqual(fallback.requests, [])

    def test_a_buffered_stream_that_ends_without_a_finish_reason_fails_over(self):
        # Rule: a buffered stream is served only once finish_reason shows it is whole; a stream
        # that ends without one (a lane that stopped mid-reply) is a failed attempt.
        dropped = self._upstream(stream_pieces=[{"content": "half a sent"}])
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._buffered_then_fallback(dropped, fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")

    def test_a_cut_off_reply_with_malformed_usage_still_fails_over(self):
        # Rule: whatever the upstream puts in usage, a cut-off reply is a failed attempt.
        cut = self._upstream(body={"model": "fake", "usage": "n/a", "choices": [
            {"message": {"content": "partial"}, "finish_reason": "length"}]})
        second = self._upstream(body=_completion("from b"))
        self._two_geminis(cut, second)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "from b")

    def test_a_cut_off_non_streaming_reply_is_logged_with_its_size_and_both_ends(self):
        # Rule: the cut-off log line is written on the non-streaming path too.
        text = "BEGIN" + "y" * 300 + "END"
        cut = self._upstream(body={"model": "fake", "usage": {"prompt_tokens": 5109,
                                                              "completion_tokens": 2048},
                                   "choices": [{"message": {"content": text},
                                                "finish_reason": "length"}]})
        self._two_geminis(cut, self._upstream(body=_completion("from b")))

        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._post({"messages": [{"role": "user", "content": "hello"}]})

        lines = [line for line in out.getvalue().splitlines() if "completion_tokens=" in line]
        self.assertEqual(len(lines), 1, out.getvalue())
        self.assertIn("tier gemini-a cut off at the token limit: prompt_tokens=5109 "
                      "prompt_chars=5 completion_tokens=2048 chars=308 head='BEGINyyy", lines[0])
        self.assertIn("yyyEND'", lines[0])

    def test_a_buffered_stream_must_finish_within_its_budget(self):
        # Rule: a buffered provider's budget covers its whole reply, since nothing reaches the
        # client before the end; a lane still generating past it fails over instead of holding
        # the lane while the client's idle timeout runs out.
        slow = self._upstream(stream_pieces=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
                              finish=("stop", {}), pace=0.6)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "buffer_stream": True,
                       "budget_s": 1},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "fallback")

    def test_a_cut_off_reply_is_served_when_no_later_tier_answers(self):
        # Rule: a whole reply from a later tier wins, but a cut-off reply beats none at all:
        # claude-mem stores its complete blocks when told finish_reason=length.
        cut = self._upstream(body={"model": "fake", "choices": [
            {"message": {"content": "<observation>a</observation><obs"},
             "finish_reason": "length"}]})
        down = self._upstream(status=503, body={"error": "down"})
        self._two_geminis(cut, down)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        choice = json.loads(raw)["choices"][0]
        self.assertEqual(choice["message"]["content"], "<observation>a</observation><obs")
        self.assertEqual(choice["finish_reason"], "length")

    def test_a_buffered_cut_off_stream_is_served_when_no_later_tier_answers(self):
        # Rule: as above, on the streaming path; the last frame says finish_reason=length.
        cut = self._upstream(stream_pieces=[{"content": "<observation>a</observation>"},
                                            {"content": "<obs"}],
                             finish=("length", {"prompt_tokens": 9, "completion_tokens": 2048}))
        down = self._upstream(status=503, body={"error": "down"})
        self._buffered_then_fallback(cut, down)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertEqual(self._streamed_text(raw), "<observation>a</observation><obs")
        frames = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith("data: {")]
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "length")
        self.assertTrue(raw.decode().rstrip().endswith("data: [DONE]"))

    def test_a_buffered_budget_holds_even_while_each_read_beats_the_read_timeout(self):
        # Rule: the whole-reply budget is a hard bound; a lane dribbling frames just inside the
        # read timeout cannot hold the request past it.
        slow = self._upstream(stream_pieces=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
                              finish=("stop", {}), pace=1.5)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "buffer_stream": True,
                       "budget_s": 2},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        started = time.time()
        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})
        elapsed = time.time() - started

        self.assertEqual(self._streamed_text(raw), "fallback")
        self.assertLess(elapsed, 2.7)  # budget 2 s; the next frame was due at 3 s

    def test_a_buffered_budget_includes_the_wait_for_headers(self):
        # Rule: the whole-reply budget starts when the request is sent, so time spent before the
        # first line (a lane holding its headers through prefill) comes out of it.
        late = self._upstream(stream_pieces=[{"content": "late"}], finish=("stop", {}),
                              delay=0.6, stall=3.0)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": late.url, "model": "lane", "buffer_stream": True,
                       "budget_s": 1},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        started = time.time()
        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})
        elapsed = time.time() - started

        self.assertEqual(self._streamed_text(raw), "fallback")
        self.assertLess(elapsed, 1.4)  # budget 1 s; a fresh read timeout would end at 1.6 s

    def test_an_unbuffered_stream_is_not_cut_at_its_budget_once_committed(self):
        # Rule: the whole-reply bound applies only to buffer_stream; a committed stream that
        # keeps arriving within the read timeout is forwarded whole.
        slow = self._upstream(stream_pieces=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
                              finish=("stop", {}), pace=0.6)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"lane": {"type": "openai", "url": slow.url, "model": "lane", "budget_s": 1},
              "fallback": {"type": "openai", "url": fallback.url, "model": "fallback"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(self._streamed_text(raw), "abc")
        self.assertEqual(fallback.requests, [])

    def _pool(self, first: FakeUpstream, second: FakeUpstream, fallback: FakeUpstream) -> None:
        router.configure(router.parse_config({
            "chain": ["pool", "fallback"],
            "providers": {
                "gemini-a": {"type": "gemini", "url": first.url, "model": "model-a",
                             "api_key_env": "TEST_GEMINI_A"},
                "gemini-b": {"type": "gemini", "url": second.url, "model": "model-b",
                             "api_key_env": "TEST_GEMINI_B"},
                "fallback": {"type": "openai", "url": fallback.url, "model": "f"},
            },
            "groups": {"pool": {"members": ["gemini-a", "gemini-b"]}},
        }))

    def test_a_group_spreads_consecutive_requests_across_its_members(self):
        first = self._upstream(body=_completion("from a"))
        second = self._upstream(body=_completion("from b"))
        fallback = self._upstream(body=_completion("fallback"))
        self._pool(first, second, fallback)

        answers = [json.loads(self._post({"messages": [{"role": "user", "content": "hi"}]})[1])
                   ["choices"][0]["message"]["content"] for _ in range(4)]

        self.assertEqual(sorted(answers), ["from a", "from a", "from b", "from b"])
        self.assertEqual(fallback.requests, [])

    def test_a_failing_member_retries_inside_the_group_before_falling_back(self):
        first = self._upstream(status=500, body={"error": "broken key"})
        second = self._upstream(body=_completion("from b"))
        fallback = self._upstream(body=_completion("fallback"))
        self._pool(first, second, fallback)

        answers = [json.loads(self._post({"messages": [{"role": "user", "content": "hi"}]})[1])
                   ["choices"][0]["message"]["content"] for _ in range(2)]

        self.assertEqual(answers, ["from b", "from b"])
        self.assertEqual(fallback.requests, [])

    def _pool_of(self, members: list[FakeUpstream], fallback: FakeUpstream, budget=12) -> None:
        names = [f"gemini-{index}" for index in range(len(members))]
        router.configure(router.parse_config({
            "chain": ["pool", "fallback"],
            "providers": {
                **{name: {"type": "gemini", "url": upstream.url, "model": name, "budget_s": budget,
                          "api_key_env": "TEST_GEMINI_A"}
                   for name, upstream in zip(names, members)},
                "fallback": {"type": "openai", "url": fallback.url, "model": "f"},
            },
            "groups": {"pool": {"members": names}},
        }))

    def test_an_overloaded_member_blocks_its_whole_group(self):
        overloaded = self._upstream(status=503, body=[{"error": {
            "code": 503, "status": "UNAVAILABLE",
            "message": "This model is currently experiencing high demand."}}])
        siblings = [self._upstream(body=_completion("sibling")) for _ in range(2)]
        fallback = self._upstream(body=_completion("fallback"))
        self._pool_of([overloaded, *siblings], fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "fallback")
        self.assertEqual([len(s.requests) for s in siblings], [0, 0])
        breaker = self._get_health()["breaker"]
        self.assertTrue(all("overloaded" in breaker[f"gemini-{i}"]["reason"] for i in range(3)),
                        breaker)

    def test_one_timeout_moves_on_to_the_next_member_without_blocking_the_group(self):
        slow = self._upstream(body=_completion("late"), delay=3.0)
        sibling = self._upstream(body=_completion("sibling"))
        fallback = self._upstream(body=_completion("fallback"))
        self._pool_of([slow, sibling], fallback, budget=1)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "sibling")
        self.assertEqual(self._get_health()["breaker"]["gemini-1"]["reason"], "")

    def test_two_members_timing_out_in_a_row_block_the_group(self):
        slow = [self._upstream(body=_completion("late"), delay=3.0) for _ in range(2)]
        sibling = self._upstream(body=_completion("sibling"))
        fallback = self._upstream(body=_completion("fallback"))
        self._pool_of([*slow, sibling], fallback, budget=1)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "fallback")
        self.assertEqual(sibling.requests, [])
        self.assertIn("overloaded", self._get_health()["breaker"]["gemini-2"]["reason"])

    # Gemini's 429 for shared model capacity: no QuotaFailure, no RetryInfo, so no reset to wait for.
    EXHAUSTED = [{"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).",
                            "status": "RESOURCE_EXHAUSTED"}}]

    def test_two_members_exhausted_in_a_row_block_the_group(self):
        # Rule: a 429 that names no quota, from two members in a row, is the model out of
        # capacity rather than one key: every member is skipped for the breaker interval.
        exhausted = [self._upstream(status=429, body=self.EXHAUSTED) for _ in range(2)]
        sibling = self._upstream(body=_completion("sibling"))
        fallback = self._upstream(body=_completion("fallback"))
        self._pool_of([*exhausted, sibling], fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "fallback")
        self.assertEqual(sibling.requests, [])
        self.assertIn("overloaded", self._get_health()["breaker"]["gemini-2"]["reason"])

    def test_a_buffered_hosted_provider_fails_over_on_a_cut_off_stream(self):
        # Rule: buffer_stream is a per-provider stream policy, not a GPU-lane gate, so a hosted
        # type accepts it too.
        cut = self._upstream(stream_pieces=[{"content": "echo"}],
                             finish=("length", {"completion_tokens": 4096}))
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        _use({"gemini-a": {"type": "gemini", "url": cut.url, "model": "m",
                           "api_key_env": "TEST_GEMINI_A", "buffer_stream": True},
              "fallback": {"type": "openai", "url": fallback.url, "model": "f"}})

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(self._streamed_text(raw), "fallback")

    def test_the_group_strike_threshold_is_configurable(self):
        # Rule: OBSERVER_GROUP_STRIKES sets how many members must fail in a row before the group
        # is blocked; with 3, two exhausted members still leave the third to answer.
        self.assertEqual(_fresh_router({"OBSERVER_GROUP_STRIKES": "3"}).GROUP_STRIKES_TO_BLOCK, 3)
        saved = router.GROUP_STRIKES_TO_BLOCK
        router.GROUP_STRIKES_TO_BLOCK = 3
        self.addCleanup(setattr, router, "GROUP_STRIKES_TO_BLOCK", saved)
        exhausted = [self._upstream(status=429, body=self.EXHAUSTED) for _ in range(2)]
        sibling = self._upstream(body=_completion("sibling"))
        self._pool_of([*exhausted, sibling], self._upstream(body=_completion("fallback")))

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "sibling")

    def test_one_member_exhausted_does_not_block_the_group(self):
        # Rule: a single unexplained 429 can be one key; its siblings are still tried.
        exhausted = self._upstream(status=429, body=self.EXHAUSTED)
        siblings = [self._upstream(body=_completion("sibling")) for _ in range(2)]
        fallback = self._upstream(body=_completion("fallback"))
        self._pool_of([exhausted, *siblings], fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "sibling")
        self.assertEqual(self._get_health()["breaker"]["gemini-2"]["reason"], "")

    def test_a_stall_after_content_is_committed_does_not_block_the_group(self):
        first = self._upstream(stream_pieces=[{"content": "hel"}], stall=0.0)
        sibling = self._upstream(stream_pieces=[{"content": "sibling"}])
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._pool_of([first, sibling], fallback, budget=1)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertIn("hel", raw.decode())
        self.assertEqual(fallback.requests, [])
        self.assertEqual(self._get_health()["breaker"]["gemini-1"]["reason"], "")

    def test_a_stream_that_stalls_after_its_headers_fails_over(self):
        stalled = self._upstream(stream_pieces=[{"content": "late"}], stall=3.0)
        fallback = self._upstream(stream_pieces=[{"content": "fallback"}])
        self._pool_of([stalled], fallback, budget=1)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}], "stream": True})

        self.assertEqual(status, 200)
        self.assertIn("fallback", raw.decode())

    def test_a_quota_429_blocks_only_that_member(self):
        exhausted = self._upstream(status=429, body=json.loads(_gemini_429(
            ["GenerateRequestsPerDayPerProjectPerModel-FreeTier"]))[0])
        sibling = self._upstream(body=_completion("sibling"))
        fallback = self._upstream(body=_completion("fallback"))
        self._pool_of([exhausted, sibling], fallback)

        status, raw = self._post({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "sibling")
        self.assertEqual(self._get_health()["breaker"]["gemini-1"]["reason"], "")

    def test_health_reports_groups_and_their_members(self):
        self._pool(self._upstream(body={}), self._upstream(body={}), self._upstream(body={}))

        health = self._get_health()

        self.assertEqual(health["chain"], ["pool", "fallback"])
        self.assertEqual(health["groups"], {"pool": ["gemini-a", "gemini-b"]})
        self.assertEqual(set(health["breaker"]), {"gemini-a", "gemini-b", "fallback"})

    def test_health_lists_each_instance_in_chain_order(self):
        self._two_geminis(self._upstream(body={}), self._upstream(body={}))

        health = self._get_health()

        self.assertEqual(health["chain"], ["gemini-a", "gemini-b"])
        self.assertEqual(health["providers"]["gemini-b"],
                         {"type": "gemini", "model": "model-b", "budget_s": 18.0})


if __name__ == "__main__":
    unittest.main()
