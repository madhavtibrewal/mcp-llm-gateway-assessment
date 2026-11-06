"""Run the router:  uv run python -m task4_model_router --port 8082"""

import argparse
import logging

import uvicorn

from .app import create_app

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8082)
args = parser.parse_args()

logging.basicConfig(level=logging.INFO)
uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
