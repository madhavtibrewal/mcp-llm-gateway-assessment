"""Start a server module as a real process on a free port, for end-to-end tests."""

import contextlib
import os
import socket
import subprocess
import sys
import time

import httpx

from tests.stdio_client import ROOT


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def run_server(module: str, *args: str, health_path: str = "/healthz", env: dict | None = None):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", module, "--port", str(port), *args],
        cwd=ROOT, env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(url + health_path, timeout=1)
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise RuntimeError(f"{module} did not start")
                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
