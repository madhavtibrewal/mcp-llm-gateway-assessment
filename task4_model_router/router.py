"""Try the primary model (3s max) and fall back to the secondary on 429, 5xx, timeout or connection errors."""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    def __init__(self, reason: str, retryable: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable  # False for 4xx other than 429, retrying a bad request elsewhere won't help


async def call_model(client: httpx.AsyncClient, url: str, model: str, body: dict, timeout: float, request_id: str) -> dict:
    try:
        # asyncio.timeout covers the whole request, including reading the body.
        # httpx's timeouts are per read, so a server sending a few bytes at a time
        # could go way past 3s without tripping them. Cancelling closes the connection too.
        async with asyncio.timeout(timeout):
            response = await client.post(f"{url}/v1/chat/completions", json={**body, "model": model, "stream": False})
    except TimeoutError:
        raise UpstreamError("timeout") from None
    except httpx.HTTPError as e:
        raise UpstreamError(f"connection error: {e!r}") from None

    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and "choices" in data:
            return data
        raise UpstreamError("malformed response")

    # log the real upstream error, the client only gets a generic one
    logger.warning("[%s] %s returned %s: %s", request_id, url, response.status_code, response.text[:500])
    retryable = response.status_code == 429 or response.status_code >= 500
    raise UpstreamError(f"HTTP {response.status_code}", retryable)


class ModelRouter:
    def __init__(self, client, primary: tuple[str, str], secondary: tuple[str, str], primary_timeout=3.0, secondary_timeout=10.0):
        self.client = client
        self.primary = primary  # (base url, model name)
        self.secondary = secondary
        self.primary_timeout = primary_timeout
        self.secondary_timeout = secondary_timeout

    async def complete(self, body: dict, request_id: str = "-") -> tuple[dict, bool]:
        """Returns (response, used_fallback)."""
        try:
            return await call_model(self.client, *self.primary, body, self.primary_timeout, request_id), False
        except UpstreamError as e:
            if not e.retryable:
                raise
            logger.warning("[%s] primary failed (%s), trying the secondary", request_id, e.reason)
        return await call_model(self.client, *self.secondary, body, self.secondary_timeout, request_id), True
