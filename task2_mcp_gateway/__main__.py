"""Run the gateway:  uv run python -m task2_mcp_gateway --port 8080"""

import argparse
import logging

import uvicorn

from .gateway import create_app

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8080)
args = parser.parse_args()

logging.basicConfig(level=logging.INFO)
uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
