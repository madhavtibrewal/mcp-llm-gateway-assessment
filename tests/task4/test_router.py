import asyncio
import json
import time

import httpx
import pytest

from task4_model_router.router import ModelRouter, UpstreamError


def completion(name: str) -> httpx.Response:
    return httpx.Response(200, json={"id": name, "choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 7}})


class FakeProviders:
    """Answers for http://primary and http://secondary, and records what each received."""

    def __init__(self, primary, secondary=None):
        self.handlers = {"primary": primary, "secondary": secondary or (lambda: completion("secondary"))}
        self.calls = {"primary": 0, "secondary": 0}
        self.primary_cancelled = False

    async def __call__(self, request: httpx.Request):
        host = request.url.host
        self.calls[host] += 1
        try:
            result = self.handlers[host]()
            return await result if asyncio.iscoroutine(result) else result
        except asyncio.CancelledError:
            self.primary_cancelled = host == "primary"
            raise


def make_router(providers, primary_timeout=3.0):
    client = httpx.AsyncClient(transport=httpx.MockTransport(providers))
    return ModelRouter(client, ("http://primary", "big"), ("http://secondary", "small"), primary_timeout, 5.0)


BODY = {"messages": [{"role": "user", "content": "hi"}]}


async def test_primary_is_used_when_healthy():
    providers = FakeProviders(lambda: completion("primary"))
    data, used_fallback = await make_router(providers).complete(BODY)
    assert data["id"] == "primary" and not used_fallback
    assert providers.calls["secondary"] == 0


def connection_refused():
    raise httpx.ConnectError("refused")


@pytest.mark.parametrize(
    "primary",
    [
        lambda: httpx.Response(429),
        lambda: httpx.Response(500, text="boom"),
        lambda: httpx.Response(200, text="<html>not json</html>"),
        connection_refused,
    ],
    ids=["429", "500", "garbage", "connection-refused"],
)
async def test_fails_over_to_the_secondary(primary):
    data, used_fallback = await make_router(FakeProviders(primary)).complete(BODY)
    assert data["id"] == "secondary" and used_fallback


async def test_client_errors_are_not_retried():
    providers = FakeProviders(lambda: httpx.Response(400, json={"error": "bad parameter"}))
    with pytest.raises(UpstreamError) as e:
        await make_router(providers).complete(BODY)
    assert not e.value.retryable
    assert providers.calls["secondary"] == 0  # it would only fail again


async def test_slow_primary_fails_over_after_3_seconds():
    async def slow():
        await asyncio.sleep(3.5)
        return completion("primary")

    providers = FakeProviders(slow)
    start = time.perf_counter()
    data, used_fallback = await make_router(providers).complete(BODY)
    elapsed = time.perf_counter() - start
    assert used_fallback and 2.95 <= elapsed < 3.4
    assert providers.primary_cancelled  # cancelled, not left running in the background


class TrickleBody(httpx.AsyncByteStream):
    """Sends the response a few bytes at a time."""

    async def __aiter__(self):
        payload = json.dumps(completion("primary").json()).encode()
        for i in range(0, len(payload), 8):
            await asyncio.sleep(0.05)
            yield payload[i:i + 8]


async def test_a_trickling_response_still_hits_the_deadline():
    # Each piece arrives quickly, but the whole body takes ~1 s. Only a deadline
    # on the whole call (not per read) catches that.
    providers = FakeProviders(lambda: httpx.Response(200, stream=TrickleBody()))
    start = time.perf_counter()
    _, used_fallback = await make_router(providers, primary_timeout=0.3).complete(BODY)
    assert used_fallback and time.perf_counter() - start < 0.8
