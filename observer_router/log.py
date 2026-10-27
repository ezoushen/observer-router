"""The router's log line: one prefix and a timestamp, flushed so launchd sees it."""

import time

LOG_PREFIX = "[observer-router]"


def log(message: str) -> None:
    print(f"{LOG_PREFIX} [{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
