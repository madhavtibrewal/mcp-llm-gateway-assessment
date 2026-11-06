"""Mock OpenAI-compatible provider that streams PII in awkward pieces.

The reply is cut into random 1-6 character deltas, and every SSE event is sent
in two network writes, so both of the guardrail's buffers get exercised.
A user message starting with "ECHO:" sets the reply text.

    uv run python -m task3_pii_guardrail.mock_llm --port 9201
"""

import argparse
import asyncio
import json
import random

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

DEFAULT_REPLY = (
    "Sure! You can reach John at john.doe@example.com, his SSN is 123-45-6789 "
    "and the card on file is 4111 1111 1111 1111 (order #1234567890123). Anything else?"
)


def create_app(delay: float = 0.02) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        last = str(body["messages"][-1].get("content", ""))
        text = last.removeprefix("ECHO:").strip() if last.startswith("ECHO:") else DEFAULT_REPLY

        if not body.get("stream"):
            return {"id": "mock-1", "object": "chat.completion", "model": "mock",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}

        def event(delta, finish=None) -> bytes:
            chunk = {"id": "mock-1", "object": "chat.completion.chunk", "model": "mock",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(chunk)}\n\n".encode()

        async def stream():
            i = 0
            while i < len(text):
                n = random.randint(1, 6)
                frame = event({"content": text[i:i + n]})
                half = random.randint(1, len(frame) - 1)
                yield frame[:half]  # one SSE event split across two writes
                yield frame[half:]
                i += n
                await asyncio.sleep(delay)
            yield event({}, finish="stop")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9201)
    parser.add_argument("--delay", type=float, default=0.02, help="seconds between deltas")
    args = parser.parse_args()
    uvicorn.run(create_app(args.delay), host="127.0.0.1", port=args.port, log_level="warning")
