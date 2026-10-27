"""Start-up: the entry script serves the router from a config file."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "observer-router.py"


class EntryPointTests(unittest.TestCase):
    """`python3 observer-router.py` is still what launchd runs."""

    def test_the_entry_script_serves_health_and_models(self):
        with socket.socket() as probe:  # a free port, released before the router binds it
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[providers.local]\ntype = "openai"\n'
                              'url = "http://127.0.0.1:9/v1"\nmodel = "m"\n')
            clean = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVER_")}
            process = subprocess.Popen(
                [sys.executable, str(MODULE)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**clean, "OBSERVER_ROUTER_CONFIG": str(config),
                     "OBSERVER_ROUTER_PORT": str(port)})
            try:
                health = self._wait_for(f"http://127.0.0.1:{port}/health", process)
                models = json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/models", timeout=5))

                self.assertEqual(health["status"], "ok")
                self.assertEqual(health["chain"], ["local"])
                self.assertEqual(models["data"][0]["id"], "observer-router")
            finally:
                process.terminate()
                process.wait(timeout=10)
                if process.stdout:
                    process.stdout.close()

    def _wait_for(self, url: str, process: subprocess.Popen) -> dict:
        deadline = time.time() + 15
        while time.time() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                self.fail(f"the router exited {process.returncode}:\n{output}")
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    return json.load(response)
            except OSError:
                time.sleep(0.2)
        self.fail(f"{url} did not answer within 15 s")


if __name__ == "__main__":
    unittest.main()
