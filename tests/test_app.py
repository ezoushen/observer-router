"""Start-up: the entry script serves the router from a config file."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "observer-router.py"

PROVIDER = '[providers.local]\ntype = "openai"\nurl = "http://127.0.0.1:9/v1"\nmodel = "m"\n'


def get_json(url: str, timeout: float = 5) -> dict:
    """Fetch a JSON body over http only; the tests never read a file or custom scheme."""
    if urllib.parse.urlsplit(url).scheme != "http":
        raise ValueError(f"not an http URL: {url}")
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


class EntryPointTests(unittest.TestCase):
    """`python3 observer-router.py` is still what launchd runs."""

    def start(self, config_text: str = PROVIDER, env: dict | None = None):
        """Start the entry script on a free port; return (process, base URL)."""
        with socket.socket() as probe:  # a free port, released before the router binds it
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Path(tmp.name) / "config.toml"
        config.write_text(config_text)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVER_")}
        process = subprocess.Popen(
            [sys.executable, str(MODULE)], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**clean, "OBSERVER_ROUTER_CONFIG": str(config),
                 "OBSERVER_ROUTER_PORT": str(port), **(env or {})})
        self.addCleanup(self._stop, process)
        return process, f"http://127.0.0.1:{port}"

    def _stop(self, process: subprocess.Popen) -> None:
        process.terminate()
        process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()

    def test_the_entry_script_serves_health_and_models(self):
        process, base = self.start()

        health = self._wait_for(f"{base}/health", process)
        models = get_json(f"{base}/v1/models")

        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["chain"], ["local"])
        self.assertEqual(models["data"][0]["id"], "observer-router")

    def test_the_retained_floor_reaches_config_validation(self):
        # Rule: OBSERVER_MIN_RETAINED_CHARS lowers the floor a provider prompt limit must clear;
        # start-up must pass the setting through, not a built-in default.
        config = ('[providers.local]\ntype = "openai"\nurl = "http://127.0.0.1:9/v1"\n'
                  'model = "m"\nmax_prompt_chars = 2047\n')

        process, base = self.start(config, {"OBSERVER_MIN_RETAINED_CHARS": "0"})
        health = self._wait_for(f"{base}/health", process)

        self.assertEqual(health["status"], "ok")

    def _wait_for(self, url: str, process: subprocess.Popen) -> dict:
        deadline = time.time() + 15
        while time.time() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                self.fail(f"the router exited {process.returncode}:\n{output}")
            try:
                return get_json(url, timeout=2)
            except OSError:
                time.sleep(0.2)
        self.fail(f"{url} did not answer within 15 s")


if __name__ == "__main__":
    unittest.main()
