import logging
import time

import httpx
import pytest

from task4_model_router.app import create_app, hash_key
from task4_model_router.limiter import TokenRateLimiter
from task4_model_router.router import ModelRouter
from tests.servers import run_server
from tests.task4.test_router import FakeProviders, completion

AUTH = {"Authorization": "Bearer tenant-a-key"}
BODY = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}


@pytest.fixture
def make_client(tmp_path):
    def make(primary=lambda: completion("primary"), secondary=None, router=None):
        limiter = TokenRateLimiter(str(tmp_path / "router.db"))
        if router is None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(FakeProviders(primary, secondary)))
            router = ModelRouter(client, ("http://primary", "big"), ("http://secondary", "small"))
        app = create_app(limiter, router)
        # raise_app_exceptions=False: let the app's own 500 handler answer, as it would in production.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        return httpx.AsyncClient(transport=transport, base_url="http://router"), limiter

    return make


def check_error(response, status, code):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert set(error) >= {"type", "code", "message", "request_id"}
    assert error["code"] == code
    assert error["request_id"] == response.headers["x-request-id"]
    return error


async def test_success_records_the_real_usage(make_client):
    client, limiter = make_client()
    response = await client.post("/v1/chat/completions", json=BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["x-gateway-fallback"] == "false"
    assert limiter.usage(hash_key("tenant-a-key")) == 7  # the estimate was replaced by usage.total_tokens


async def test_fallback_is_invisible_to_the_client(make_client):
    client, _ = make_client(primary=lambda: httpx.Response(429))
    response = await client.post("/v1/chat/completions", json=BODY, headers=AUTH)
    assert response.json()["id"] == "secondary"
    assert response.headers["x-gateway-fallback"] == "true"


async def test_bad_api_key_is_401(make_client):
    client, _ = make_client()
    check_error(await client.post("/v1/chat/completions", json=BODY), 401, "invalid_api_key")
    check_error(await client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer nope"}), 401, "invalid_api_key")


async def test_exceeding_the_budget_is_429_with_retry_after(make_client):
    big = {**BODY, "max_tokens": 20_000}
    usage = lambda: httpx.Response(200, json={"id": "p", "choices": [], "usage": {"total_tokens": 20_000}})
    client, _ = make_client(primary=usage)
    for _ in range(2):
        assert (await client.post("/v1/chat/completions", json=big, headers=AUTH)).status_code == 200
    response = await client.post("/v1/chat/completions", json=big, headers=AUTH)
    check_error(response, 429, "rate_limit_exceeded")
    assert 1 <= int(response.headers["retry-after"]) <= 60


async def test_request_bigger_than_the_whole_budget_is_400(make_client):
    client, _ = make_client()
    response = await client.post("/v1/chat/completions", json={**BODY, "max_tokens": 60_000}, headers=AUTH)
    check_error(response, 400, "request_too_large")


async def test_upstream_failure_is_503_without_details(make_client, caplog):
    trace = lambda: httpx.Response(500, text="Traceback ... KeyError: 'api_key=sk-live-SECRET'")
    client, limiter = make_client(primary=trace, secondary=trace)
    with caplog.at_level(logging.WARNING):
        response = await client.post("/v1/chat/completions", json=BODY, headers=AUTH)
    error = check_error(response, 503, "upstream_unavailable")
    assert "sk-live" not in response.text and "Traceback" not in response.text
    assert any(error["request_id"] in r.getMessage() and "sk-live" in r.getMessage() for r in caplog.records)  # but it is logged
    assert limiter.usage(hash_key("tenant-a-key")) == 0  # the failed request gave its tokens back


async def test_unexpected_exception_is_a_plain_500(make_client):
    class BrokenRouter:
        async def complete(self, body, request_id):
            raise RuntimeError("db password=hunter2")

    client, _ = make_client(router=BrokenRouter())
    response = await client.post("/v1/chat/completions", json=BODY, headers=AUTH)
    error = check_error(response, 500, "internal_error")
    assert error["message"] == "An internal error occurred." and "hunter2" not in response.text


@pytest.mark.parametrize("body", [{"messages": []}, {"model": "x"}, {"messages": [{"role": "user"}], "max_tokens": -5}])
async def test_invalid_body_is_400_and_not_echoed(make_client, body):
    client, _ = make_client()
    check_error(await client.post("/v1/chat/completions", json=body, headers=AUTH), 400, "invalid_body")


def test_real_servers_fail_over_on_a_slow_primary(tmp_path):
    with run_server("task4_model_router.mock_provider", "--name", "primary", "--mode", "slow") as primary, \
         run_server("task4_model_router.mock_provider", "--name", "secondary") as secondary:
        env = {"ROUTER_DB_PATH": str(tmp_path / "e2e.db"), "ROUTER_PRIMARY_URL": primary, "ROUTER_SECONDARY_URL": secondary}
        with run_server("task4_model_router", env=env) as router:
            start = time.perf_counter()
            response = httpx.post(f"{router}/v1/chat/completions", json=BODY, headers=AUTH, timeout=20)
            elapsed = time.perf_counter() - start
    assert response.headers["x-gateway-fallback"] == "true"
    assert response.json()["choices"][0]["message"]["content"] == "Hello from secondary."
    assert 2.9 <= elapsed < 4.0
