"""The provider and chain schema: parsing, defaults, rejection and warnings."""

import pathlib
import tempfile
import unittest

from observer_router.config import (
    Config,
    ConfigError,
    chain_override,
    load_config,
    parse_config,
    provider_warnings,
    stale_environment,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _config(text: str) -> Config:
    """Parse a TOML config through the public loader, via a temporary file."""
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "config.toml"
        path.write_text(text)
        return load_config(str(path))


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

    def test_a_reject_list_and_a_minimum_budget_are_read_for_any_type(self):
        # Rule: both are per-provider routing policy, not GPU-lane gates, so a hosted type
        # takes them too; unset, nothing is rejected and only the one-second floor applies.
        config = _config('[providers.x]\ntype = "openrouter"\nmodel = "openrouter/free"\n'
                         'api_key_env = "K"\nreject_models = ["a/safety:free"]\n'
                         'min_budget_s = 5\n'
                         '[providers.y]\ntype = "gemini"\nmodel = "m"\napi_key_env = "K"\n')

        self.assertEqual(config.providers["x"].reject_models, ("a/safety:free",))
        self.assertEqual(config.providers["x"].min_budget, 5.0)
        self.assertEqual(config.providers["y"].reject_models, ())
        self.assertEqual(config.providers["y"].min_budget, 1.0)

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
            "prompt limit not an integer": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\n'
                                            'model = "m"\nmax_prompt_chars = "96000"\n',
                                            "max_prompt_chars must be"),
            "prompt limit below the retained floor": (
                '[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                'max_prompt_chars = 2047\n', "max_prompt_chars must be at least"),
            "breaker threshold zero": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\n'
                                       'model = "m"\nbreak_after = 0\n', "break_after must be"),
            "breaker threshold boolean": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\n'
                                          'model = "m"\nbreak_after = true\n',
                                          "break_after must be"),
            "breaker seconds a string": ('[providers.x]\ntype = "gemini"\nmodel = "m"\n'
                                         'api_key_env = "K"\nbreak_seconds = "60"\n',
                                         "break_seconds must be"),
            "reject list as a string": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\n'
                                        'model = "m"\nreject_models = "a/b"\n',
                                        "reject_models must be"),
            "reject list with an empty id": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\n'
                                             'model = "m"\nreject_models = [""]\n',
                                             "reject_models must be"),
            "minimum budget zero": ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\n'
                                    'model = "m"\nmin_budget_s = 0\n', "min_budget_s must be"),
            "minimum budget under the one-second floor": (
                '[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                'min_budget_s = 0.2\n', "min_budget_s must be at least 1"),
            "minimum budget above the budget": (
                '[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                'budget_s = 30\nmin_budget_s = 31\n', "min_budget_s must not exceed"),
            "gate on a remote type": (
                '[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "K"\nserial = true\n',
                "serial",
            ),
        }
        for label, (text, needle) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ConfigError) as caught:
                    _config(text)
                self.assertIn(needle, str(caught.exception))

    def test_secret_values_never_appear_in_errors(self):
        with self.assertRaises(ConfigError) as caught:
            _config('[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key = "AIza-secret"\n')

        self.assertNotIn("AIza-secret", str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            _config('[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "sk-or-v1-abc"\n')
        self.assertNotIn("sk-or-v1-abc", str(caught.exception))

    def test_the_retained_floor_follows_the_router_setting(self):
        # Rule: the guard keeps a message only if it can keep the configured minimum, so the
        # provider floor moves with OBSERVER_MIN_RETAINED_CHARS.
        low = parse_config({"providers": {"x": {"type": "openai", "url": "http://h/v1",
                                                "model": "m", "max_prompt_chars": 2047}}},
                           min_retained_chars=0)

        self.assertEqual(low.providers["x"].max_prompt_chars, 2047)

    def test_the_shipped_example_is_a_valid_config(self):
        config = load_config(str(ROOT / "config.example.toml"))

        self.assertEqual(config.chain, ("gemini", "openrouter", "cursor", "local"))
        self.assertEqual(config.groups["gemini"], ("gemini-a", "gemini-b"))

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
                with self.assertRaises(ConfigError) as caught:
                    _config(chain + text + providers)
                self.assertIn(needle, str(caught.exception))

    def test_invalid_idle_signals_are_rejected_with_the_reason(self):
        base = ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                'idle_metrics = ["http://h:1/metrics"]\n')
        cases = {
            "without idle_metrics": (
                '[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                'idle_signal = {in_flight = ["a"]}\n', "idle_signal needs idle_metrics"),
            "both shapes": (base + 'idle_signal = {in_flight = ["a"], submitted = "b", '
                                   'finished = ["c"]}\n', "exactly one of"),
            "neither shape": (base + 'idle_signal = {}\n', "exactly one of"),
            "unknown key": (base + 'idle_signal = {in_flight = ["a"], extra = 1}\n',
                            "unknown key"),
            "counters need finished": (base + 'idle_signal = {submitted = "a"}\n', "finished"),
            "names must be metric names": (base + 'idle_signal = {in_flight = ["not a name"]}\n',
                                           "metric name"),
            "a name listed twice": (base + 'idle_signal = {submitted = "a", finished = ["b", "b"]}\n',
                                    "more than once"),
            "names must be a list": (base + 'idle_signal = {in_flight = "a"}\n', "list"),
            "on a hosted type": (
                '[providers.x]\ntype = "gemini"\nmodel = "m"\napi_key_env = "K"\n'
                'idle_signal = {in_flight = ["a"]}\n', "for type gemini"),
            "one url read two ways": (
                base + 'idle_signal = {in_flight = ["a"]}\n'
                '[providers.y]\ntype = "openai"\nurl = "http://h/v2"\nmodel = "m"\n'
                'idle_metrics = ["http://h:1/metrics"]\n', "read two ways"),
        }
        for label, (text, needle) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ConfigError) as caught:
                    _config(text)
                self.assertIn(needle, str(caught.exception))

    def test_load_config_passes_the_retained_floor(self):
        # Rule: the provider floor follows the router setting, and the loader takes it.
        text = ('[providers.x]\ntype = "openai"\nurl = "http://h/v1"\nmodel = "m"\n'
                'max_prompt_chars = 2047\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            path.write_text(text)
            low = load_config(str(path), min_retained_chars=0)
            with self.assertRaises(ConfigError):
                load_config(str(path))

        self.assertEqual(low.providers["x"].max_prompt_chars, 2047)

    def test_removed_environment_variables_are_named(self):
        stale = stale_environment({"OBSERVER_LOCAL_URL": "x", "OBSERVER_ROUTER_PORT": "1",
                                   "OBSERVER_CURSOR_MODEL": "y", "HOME": "/h"})

        self.assertEqual(stale, ["OBSERVER_CURSOR_MODEL", "OBSERVER_LOCAL_URL"])

    def test_chain_override_selects_and_orders_instances(self):
        config = _config(TWO_GEMINIS)

        self.assertEqual(chain_override(config, "local, gemini-a"), ("local", "gemini-a"))
        self.assertEqual(chain_override(config, ""), config.chain)
        with self.assertRaises(ConfigError):
            chain_override(config, "gemini-a,nope")


class ProviderWarningTests(unittest.TestCase):
    """Settings that parse but never act are named at start-up."""

    def test_a_provider_limit_the_router_wide_guard_overrides_is_reported(self):
        config = parse_config({"providers": {
            "loose": {"type": "openai", "url": "http://h/v1", "model": "m",
                      "max_prompt_chars": 240_001},
            "tight": {"type": "openai", "url": "http://h/v1", "model": "m",
                      "max_prompt_chars": 240_000}}})

        warnings = provider_warnings(config.providers, max_prompt_chars=240_000)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("loose", warnings[0])
        self.assertIn("OBSERVER_MAX_PROMPT_CHARS", warnings[0])

    def test_ac_only_on_another_host_is_warned_about(self):
        # Rule: ac_only reads this machine's battery, so on a provider whose url names another
        # host it cannot see that host's power; start-up says so.
        config = parse_config({"providers": {
            "remote-lane": {"type": "openai", "url": "http://192.0.2.7:8030/v1", "model": "m",
                            "ac_only": True},
            "local-lane": {"type": "openai", "url": "http://127.0.0.1:1240/v1", "model": "m",
                           "ac_only": True},
            "unlimited": {"type": "openai", "url": "http://192.0.2.7:8030/v1", "model": "m"}}})

        warnings = provider_warnings(config.providers)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("remote-lane", warnings[0])
        self.assertIn("battery", warnings[0])


if __name__ == "__main__":
    unittest.main()
