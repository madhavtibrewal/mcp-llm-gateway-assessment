"""Fake provider for trying the router.

Modes: ok, rate_limit (429), error (500 with a traceback in the body), slow (3.5s).
Use --mode for the whole process, or {"mock": {"primary": "slow"}} in a request body.

    uv run python -m task4_model_router.mock_provider --name primary --port 9301
"""

import argparse
import asyncio

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse


def create_app(name: str, mode: str = "ok") -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        current = body.get("mock", {}).get(name, mode)
        if current == "rate_limit":
            return JSONResponse({"error": "rate limit reached for org-123"}, status_code=429)
        if current == "error":
            return PlainTextResponse("Traceback (most recent call last):\n  KeyError: 'api_key=sk-live-SECRET'", status_code=500)
        if current == "slow":
            await asyncio.sleep(3.5)
        # pretend we used all of max_tokens, so it's easy to hit the limit with curl
        completion_tokens = body.get("max_tokens") or 5
        return {
            "id": f"cmpl-{name}",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"Hello from {name}."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens, "total_tokens": 10 + completion_tokens},
        }

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="primary")
    parser.add_argument("--port", type=int, default=9301)
    parser.add_argument("--mode", choices=["ok", "rate_limit", "error", "slow"], default="ok")
    args = parser.parse_args()
    uvicorn.run(create_app(args.name, args.mode), host="127.0.0.1", port=args.port, log_level="warning")
