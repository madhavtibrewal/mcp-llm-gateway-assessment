"""Task 3 - OpenAI-style /v1/chat/completions proxy that redacts PII from the
response, including while it's streaming."""

import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .redactor import redact
from .stream import redact_stream


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message}}, status_code=status)


def upstream_error(status: int) -> JSONResponse:
    # don't pass the upstream error body through, it can leak internals
    if status == 429:
        return error(429, "The model provider is rate limiting requests.")
    if 400 <= status < 500:
        return error(400, "The model provider rejected the request.")
    return error(502, "The model provider returned an error.")


def create_app(upstream_url: str | None = None, client: httpx.AsyncClient | None = None) -> FastAPI:
    base = upstream_url or os.environ.get("LLM_UPSTREAM_URL", "http://127.0.0.1:9201")
    url = base.rstrip("/") + "/v1/chat/completions"
    # the read timeout is per chunk, so long streams are fine
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return error(400, "Request body must be JSON.")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return error(400, "'messages' must be a list.")

        if body.get("stream"):
            try:
                upstream = await client.send(client.build_request("POST", url, json=body), stream=True)
            except httpx.HTTPError:
                return error(502, "The model provider is unavailable.")
            if upstream.status_code != 200:
                await upstream.aclose()
                return upstream_error(upstream.status_code)

            async def relay():
                try:
                    async for event in redact_stream(upstream.aiter_bytes()):
                        yield event
                finally:
                    await upstream.aclose()  # also runs if the client disconnects

            return StreamingResponse(relay(), media_type="text/event-stream")

        try:
            upstream = await client.post(url, json=body)
        except httpx.HTTPError:
            return error(502, "The model provider is unavailable.")
        if upstream.status_code != 200:
            return upstream_error(upstream.status_code)
        data = upstream.json()
        for choice in data.get("choices", []):
            message = choice.get("message") or {}
            if isinstance(message.get("content"), str):
                message["content"] = redact(message["content"])
        return data

    return app
