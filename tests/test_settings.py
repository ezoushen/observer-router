"""The router-wide OBSERVER_* settings: defaults, values, validation and start-up handling."""

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from observer_router.settings import Settings, build_settings

MODULE_PATH = Path(__file__).resolve().parents[1] / "observer-router.py"


class SettingsTests(unittest.TestCase):
    """Router-wide settings are read from one mapping and validated once."""

    def test_defaults_apply_when_nothing_is_set(self):
        # Rule: every knob has the documented default, so an empty environment still starts.
        settings, errors = build_settings({})

        self.assertEqual(settings, Settings(
            host="127.0.0.1", port=1244, allowed_hosts=(),
            config_path=os.path.expanduser("~/.config/observer-router/config.toml"),
            break_after=3, break_seconds=60.0, max_prompt_chars=240_000,
            max_field_chars=80_000, min_retained_chars=1_024, base64_blob_chars=16_384,
            power_cache_seconds=30.0, idle_sample_seconds=2.0, group_strikes=2,
            request_deadline_seconds=85.0,
        ))
        self.assertEqual(errors, ())

    def test_values_are_read_from_the_environment(self):
        # Rule: each variable sets its knob; an empty numeric knob uses its default (the two
        # path/address variables that keep an empty value are covered below).
        settings, errors = build_settings({
            "OBSERVER_ROUTER_HOST": "127.0.0.2",
            "OBSERVER_ROUTER_PORT": "1300",
            "OBSERVER_BREAK_AFTER": "5",
            "OBSERVER_BREAK_SECONDS": "2.5",
            "OBSERVER_MAX_PROMPT_CHARS": "1000",
            "OBSERVER_MAX_FIELD_CHARS": "200",
            "OBSERVER_MIN_RETAINED_CHARS": "0",
            "OBSERVER_BASE64_BLOB_CHARS": "64",
            "OBSERVER_POWER_CACHE_SECONDS": "1.5",
            "OBSERVER_IDLE_SAMPLE_SECONDS": "0.5",
            "OBSERVER_GROUP_STRIKES": "4",
            "OBSERVER_REQUEST_DEADLINE_SECONDS": "60",
            "OBSERVER_ROUTER_CONFIG": "~/custom.toml",
            "OBSERVER_ROUTER_ALLOWED_HOSTS": " Router.LAN , nas.local,",
        })

        self.assertEqual(errors, ())
        self.assertEqual(settings.host, "127.0.0.2")
        self.assertEqual(settings.port, 1300)
        self.assertEqual(settings.break_after, 5)
        self.assertEqual(settings.break_seconds, 2.5)
        self.assertEqual(settings.max_prompt_chars, 1000)
        self.assertEqual(settings.max_field_chars, 200)
        self.assertEqual(settings.min_retained_chars, 0)
        self.assertEqual(settings.base64_blob_chars, 64)
        self.assertEqual(settings.power_cache_seconds, 1.5)
        self.assertEqual(settings.idle_sample_seconds, 0.5)
        self.assertEqual(settings.group_strikes, 4)
        self.assertEqual(settings.request_deadline_seconds, 60.0)
        self.assertEqual(settings.config_path, os.path.expanduser("~/custom.toml"))
        self.assertEqual(settings.allowed_hosts, ("router.lan", "nas.local"))

    def test_an_empty_variable_uses_the_default(self):
        # Rule: a plist may set a variable to the empty string; that is "unset", not zero.
        settings, errors = build_settings({"OBSERVER_ROUTER_PORT": "  ", "OBSERVER_BREAK_AFTER": ""})

        self.assertEqual(errors, ())
        self.assertEqual(settings.port, 1244)
        self.assertEqual(settings.break_after, 3)

    def test_an_invalid_setting_names_the_variable_and_keeps_the_default(self):
        # Rule: a value that is not a number, or out of range, is an error at start-up naming the
        # variable, never a silent fallback.
        cases = {
            "not a number": ("OBSERVER_BREAK_AFTER", "three", "break_after", 3),
            "zero strikes": ("OBSERVER_GROUP_STRIKES", "0", "group_strikes", 2),
            "deadline past a lock's longest wait": ("OBSERVER_REQUEST_DEADLINE_SECONDS", "1e10",
                                                    "request_deadline_seconds", 85.0),
            "deadline under a second": ("OBSERVER_REQUEST_DEADLINE_SECONDS", "0.5",
                                        "request_deadline_seconds", 85.0),
            "negative seconds": ("OBSERVER_BREAK_SECONDS", "-1", "break_seconds", 60.0),
            "infinite seconds": ("OBSERVER_IDLE_SAMPLE_SECONDS", "inf", "idle_sample_seconds", 2.0),
            "port out of range": ("OBSERVER_ROUTER_PORT", "70000", "port", 1244),
            "zero prompt budget": ("OBSERVER_MAX_PROMPT_CHARS", "0", "max_prompt_chars", 240_000),
            "integer too big for a float": ("OBSERVER_MAX_PROMPT_CHARS", "9" * 400,
                                            "max_prompt_chars", 240_000),
            "blob size past the regex limit": ("OBSERVER_BASE64_BLOB_CHARS", "99999999999",
                                               "base64_blob_chars", 16_384),
            "sample interval too long to sleep": ("OBSERVER_IDLE_SAMPLE_SECONDS", "1e30",
                                                  "idle_sample_seconds", 2.0),
        }
        for label, (name, value, attribute, default) in cases.items():
            with self.subTest(label):
                settings, errors = build_settings({name: value})
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(name, errors[0])
                self.assertEqual(getattr(settings, attribute), default)

    def test_an_empty_host_is_kept_as_the_empty_address(self):
        # Rule: like the config path, an empty OBSERVER_ROUTER_HOST keeps the empty address
        # instead of the default (as before the split).
        settings, errors = build_settings({"OBSERVER_ROUTER_HOST": ""})

        self.assertEqual(errors, ())
        self.assertEqual(settings.host, "")

    def test_an_empty_config_path_is_not_the_default(self):
        # Rule: only an absent OBSERVER_ROUTER_CONFIG takes the default; an empty one is the empty
        # path, which start-up reports as unreadable (as before the split).
        settings, errors = build_settings({"OBSERVER_ROUTER_CONFIG": ""})

        self.assertEqual(errors, ())
        self.assertEqual(settings.config_path, "")

    def test_an_invalid_allowed_host_name_is_refused_whole(self):
        # Rule: one bad entry rejects the list rather than silently serving no names or a typo.
        settings, errors = build_settings({"OBSERVER_ROUTER_ALLOWED_HOSTS": "ok.lan,bad host"})

        self.assertIn("OBSERVER_ROUTER_ALLOWED_HOSTS", errors[0])
        self.assertEqual(settings.allowed_hosts, ())

    def test_an_invalid_setting_stops_the_router_and_names_it(self):
        # Rule: start-up exits 2 and prints the variable, like a bad config file.
        with socket.socket() as probe:  # a free port, released before the router binds it
            probe.bind(("127.0.0.1", 0))
            port = str(probe.getsockname()[1])
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[providers.x]\ntype = "openai"\nurl = "http://127.0.0.1:9/v1"\n'
                              'model = "m"\n')
            clean = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVER_")}
            try:
                result = subprocess.run(
                    [sys.executable, str(MODULE_PATH)], capture_output=True, text=True, timeout=60,
                    env={**clean, "OBSERVER_ROUTER_CONFIG": str(config),
                         "OBSERVER_ROUTER_PORT": port,
                         "OBSERVER_BREAK_AFTER": "three"},
                )
            except subprocess.TimeoutExpired:
                self.fail("the router did not exit within 60 s; it may have started")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("OBSERVER_BREAK_AFTER", result.stdout)


if __name__ == "__main__":
    unittest.main()
