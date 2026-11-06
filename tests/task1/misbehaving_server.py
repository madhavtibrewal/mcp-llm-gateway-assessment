"""Test-only: the real server with a backend that misbehaves.

    noisy: the backend writes to stdout, which would corrupt a stdio MCP stream
    crash: the backend raises an unexpected exception
"""

import os
import sys

from task1_mcp_server import server


class NoisyCustomers(dict):
    def get(self, key, default=None):
        print("STRAY PRINT")
        os.write(1, b"RAW FD WRITE\n")  # below Python, like a C extension would
        return super().get(key, default)


class CrashingCustomers(dict):
    def get(self, key, default=None):
        raise RuntimeError("db password=hunter2")


if __name__ == "__main__":
    broken = NoisyCustomers if sys.argv[1] == "noisy" else CrashingCustomers
    server.CUSTOMERS = broken(server.CUSTOMERS)
    server.main()
