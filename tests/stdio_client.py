"""Runs an MCP server as a subprocess and talks raw JSON-RPC to it over stdio.

Not using the SDK client here because the tests need to see exactly what the
server writes to stdout.
"""

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StdioServer:
    def __init__(self, *args: str):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", *args], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.stdout_lines: list[bytes] = []
        self.stderr = b""
        self._lines: queue.Queue = queue.Queue()
        self._next_id = 0
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for t in self._readers:
            t.start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            self.stdout_lines.append(line)
            self._lines.put(line)

    def _read_stderr(self):
        self.stderr = self.proc.stderr.read()

    def send(self, message: dict):
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}})
        while True:
            response = json.loads(self._lines.get(timeout=15))
            if response.get("id") == self._next_id:
                return response

    def initialize(self, version: str = "2025-11-25") -> dict:
        response = self.request(
            "initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        for t in self._readers:
            t.join(timeout=5)

    def assert_stdout_is_only_jsonrpc(self):
        assert self.stdout_lines
        for line in self.stdout_lines:
            assert json.loads(line)["jsonrpc"] == "2.0", line
