import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from task3_pii_guardrail.app import create_app
from task3_pii_guardrail.mock_llm import DEFAULT_REPLY
from task3_pii_guardrail.mock_llm import create_app as create_mock_llm
from task3_pii_guardrail.redactor import redact
from tests.servers import run_server
from tests.task3.test_stream import content_of, split_events

REQUEST = {"model": "mock", "messages": [{"role": "user", "content": "hi"}]}


def client_for(upstream_app) -> httpx.AsyncClient:
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream_app))
    app = create_app("http://llm", upstream)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


async def test_streamed_reply_is_redacted():
    async with client_for(create_mock_llm(delay=0)) as client:
        response = await client.post("/v1/chat/completions", json={**REQUEST, "stream": True})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert content_of(split_events(response.content)[0]) == redact(DEFAULT_REPLY)


async def test_non_streamed_reply_is_redacted():
    async with client_for(create_mock_llm()) as client:
        response = await client.post("/v1/chat/completions", json=REQUEST)
    assert response.json()["choices"][0]["message"]["content"] == redact(DEFAULT_REPLY)


@pytest.mark.parametrize("stream", [True, False])
async def test_upstream_errors_do_not_leak_details(stream):
    broken = FastAPI()

    @broken.post("/v1/chat/completions")
    async def fail():
        return PlainTextResponse("Traceback ... api_key=sk-live-123", status_code=500)

    async with client_for(broken) as client:
        response = await client.post("/v1/chat/completions", json={**REQUEST, "stream": stream})
    assert response.status_code == 502
    assert "sk-live" not in response.text


def test_real_servers_stream_incrementally():
    with run_server("task3_pii_guardrail.mock_llm", "--delay", "0.03") as llm:
        with run_server("task3_pii_guardrail", env={"LLM_UPSTREAM_URL": llm}) as gateway:
            arrivals, raw, start = [], b"", time.perf_counter()
            with httpx.stream("POST", f"{gateway}/v1/chat/completions", json={**REQUEST, "stream": True}, timeout=30) as response:
                for piece in response.iter_raw():
                    arrivals.append(time.perf_counter() - start)
                    raw += piece
    assert content_of(split_events(raw)[0]) == redact(DEFAULT_REPLY)
    # The first bytes arrive long before the stream ends, so nothing is buffered whole.
    assert arrivals[0] < 0.5 * arrivals[-1]
