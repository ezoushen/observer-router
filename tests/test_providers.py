"""Request rewriting: the payload per type, the key lookup and the headers."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from observer_router.config import Provider
from observer_router.failure import TierFailure
from observer_router.providers import api_key, headers_for, payload_for


def _provider(**changes) -> Provider:
    fields = {"name": "p", "type": "openai", "url": "http://127.0.0.1:9/v1",
              "model": "m", "budget": 40.0}
    fields.update(changes)
    return Provider(**fields)


class PayloadTests(unittest.TestCase):
    """payload_for rewrites a request for one provider instance."""

    def test_the_instance_model_and_sampling_fields_are_used(self):
        body = {"messages": [{"role": "user", "content": "x"}], "max_tokens": 5, "seed": 1}

        payload = payload_for(_provider(model="composer-2.5"), body, None)

        self.assertEqual(payload["model"], "composer-2.5")
        self.assertEqual(payload["max_tokens"], 5)
        self.assertNotIn("seed", payload)
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_stream_is_a_boolean(self):
        self.assertTrue(payload_for(_provider(), {"messages": [], "stream": True}, None)["stream"])
        self.assertFalse(payload_for(_provider(), {"messages": []}, None)["stream"])

    def test_chat_template_kwargs_are_sent_as_configured(self):
        provider = _provider(chat_template_kwargs={"enable_thinking": False})

        self.assertEqual(payload_for(provider, {"messages": []}, None)["chat_template_kwargs"],
                         {"enable_thinking": False})
        self.assertNotIn("chat_template_kwargs",
                         payload_for(_provider(), {"messages": []}, None))

    def test_reasoning_effort_only_when_configured(self):
        self.assertEqual(
            payload_for(_provider(reasoning_effort="none"), {"messages": []}, None)
            ["reasoning_effort"], "none")

    def test_openrouter_reasoning_modes(self):
        provider = _provider(type="openrouter", api_key_env="K")

        self.assertEqual(payload_for(provider, {"messages": []}, "disabled")["reasoning"],
                         {"enabled": False})
        self.assertEqual(payload_for(provider, {"messages": []}, "exclude")["reasoning"],
                         {"exclude": True})

    def test_a_remote_type_gets_no_reasoning_field(self):
        self.assertNotIn("reasoning",
                         payload_for(_provider(type="gemini", api_key_env="K"),
                                     {"messages": []}, "disabled"))


class KeyTests(unittest.TestCase):
    """A key comes from an environment variable or from a file the provider names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _file(self, name: str, text: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_text(text)
        return str(path)

    def test_key_from_the_named_environment_variable(self):
        os.environ["TEST_KEY_A"] = "k"

        self.addCleanup(os.environ.pop, "TEST_KEY_A", None)
        self.assertEqual(api_key(_provider(type="gemini", api_key_env="TEST_KEY_A")), "k")

    def test_key_from_a_dotenv_file(self):
        path = self._file("k.env", "K=abc\n")

        self.assertEqual(api_key(_provider(api_key_file=path, api_key_var="K")), "abc")

    def test_key_from_a_json_settings_file(self):
        path = self._file("settings.json", json.dumps({"K": "abc"}))

        self.assertEqual(api_key(_provider(api_key_file=path, api_key_var="K")), "abc")

    def test_environment_wins_over_the_file(self):
        os.environ["TEST_KEY_A"] = "env-key"
        self.addCleanup(os.environ.pop, "TEST_KEY_A", None)
        path = self._file("k.env", "K=file-key\n")

        self.assertEqual(api_key(_provider(api_key_env="TEST_KEY_A", api_key_file=path,
                                           api_key_var="K")), "env-key")

    def test_dotenv_last_assignment_wins_and_inline_comments_are_dropped(self):
        path = self._file("k.env", "K=old\nK=new # rotated\nQ='quoted # kept'\nR=\"r\" # note\n")

        self.assertEqual(api_key(_provider(api_key_file=path, api_key_var="K")), "new")
        self.assertEqual(api_key(_provider(api_key_file=path, api_key_var="Q")), "quoted # kept")
        self.assertEqual(api_key(_provider(api_key_file=path, api_key_var="R")), "r")

    def test_a_missing_key_fails_the_provider_without_naming_a_value(self):
        with self.assertRaises(TierFailure) as caught:
            headers_for(_provider(type="gemini", api_key_env="TEST_ABSENT_KEY"))

        self.assertIn("p:", str(caught.exception))
        self.assertIn("TEST_ABSENT_KEY", str(caught.exception))

    def test_a_key_with_a_line_break_fails_without_echoing_it(self):
        os.environ["TEST_KEY_A"] = "sk-SECRET\nsecond-line"
        self.addCleanup(os.environ.pop, "TEST_KEY_A", None)

        with self.assertRaises(TierFailure) as caught:
            headers_for(_provider(type="gemini", api_key_env="TEST_KEY_A"))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_an_unreadable_key_file_is_named_as_the_cause(self):
        provider = _provider(type="gemini", api_key_file=str(Path(self.tmp.name) / "absent.env"),
                             api_key_var="K")

        with self.assertRaises(TierFailure) as caught:
            headers_for(provider)
        self.assertIn("cannot read", str(caught.exception))


class HeaderTests(unittest.TestCase):
    """headers_for sends the key and the attribution the type needs."""

    def test_keyless_openai_provider_sends_no_authorization(self):
        self.assertNotIn("Authorization", headers_for(_provider()))

    def test_a_key_is_sent_as_a_bearer(self):
        os.environ["TEST_KEY_A"] = "k"
        self.addCleanup(os.environ.pop, "TEST_KEY_A", None)

        self.assertEqual(headers_for(_provider(type="gemini", api_key_env="TEST_KEY_A"))
                         ["Authorization"], "Bearer k")

    def test_openrouter_sends_attribution_headers(self):
        os.environ["TEST_KEY_A"] = "k"
        self.addCleanup(os.environ.pop, "TEST_KEY_A", None)
        provider = _provider(type="openrouter", api_key_env="TEST_KEY_A",
                             site_url="https://x.test", app_name="me")

        headers = headers_for(provider)

        self.assertEqual(headers["HTTP-Referer"], "https://x.test")
        self.assertEqual(headers["X-Title"], "me")


if __name__ == "__main__":
    unittest.main()
