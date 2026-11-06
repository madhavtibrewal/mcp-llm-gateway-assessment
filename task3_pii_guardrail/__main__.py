"""Run the guardrail:  uv run python -m task3_pii_guardrail --port 8081"""

import argparse
import logging

import uvicorn

from .app import create_app

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8081)
args = parser.parse_args()

logging.basicConfig(level=logging.INFO)
uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
