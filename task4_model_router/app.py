"""Task 4 - /v1/chat/completions with per-tenant token limits and primary/secondary fallback.

All errors look like {"error": {"type", "code", "message", "request_id"}}.
Upstream error bodies and tracebacks only go to the logs.
"""

import hashlib
import logging
import math
import os
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from .limiter import RateLimited, TokenRateLimiter
from .router import ModelRouter, UpstreamError

logger = logging.getLogger(__name__)

TENANT_KEYS = os.environ.get("ROUTER_TENANT_KEYS", "tenant-a-key,tenant-b-key").split(",")
DEFAULT_MAX_TOKENS = 1024


class GatewayError(Exception):
    def __init__(self, status: int, type_: str, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.type, self.code, self.message, self.headers = status, type_, code, message, headers or {}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # temperature etc. are passed through
    messages: list[dict] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False


def hash_key(key: str) -> str:
    # store a hash in the DB, not the key itself
    return hashlib.sha256(key.encode()).hexdigest()


def estimate_tokens(body: dict) -> int:
    # rough estimate (~4 chars per token) + max_tokens. Gets replaced with the
    # real usage once the model responds.
    prompt_chars = sum(len(str(m.get("content", ""))) for m in body["messages"])
    return prompt_chars // 4 + (body.get("max_tokens") or DEFAULT_MAX_TOKENS)


def error_response(request: Request, status: int, type_: str, code: str, message: str, headers=None, **extra):
    request_id = request.state.request_id
    body = {"type": type_, "code": code, "message": message, "request_id": request_id, **extra}
    return JSONResponse({"error": body}, status_code=status, headers={**(headers or {}), "X-Request-ID": request_id})


def create_app(limiter: TokenRateLimiter | None = None, router: ModelRouter | None = None) -> FastAPI:
    limiter = limiter or TokenRateLimiter(os.environ.get("ROUTER_DB_PATH", "router.db"))
    router = router or ModelRouter(
        httpx.AsyncClient(timeout=30),
        primary=(os.environ.get("ROUTER_PRIMARY_URL", "http://127.0.0.1:9301"), "primary-large"),
        secondary=(os.environ.get("ROUTER_SECONDARY_URL", "http://127.0.0.1:9302"), "backup-small"),
    )
    tenants = {hash_key(k) for k in TENANT_KEYS}
    app = FastAPI()

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request.state.request_id = f"req_{uuid.uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request, e: GatewayError):
        return error_response(request, e.status, e.type, e.code, e.message, e.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, e: RequestValidationError):
        # field + problem, without echoing the submitted value
        fields = [{"field": ".".join(str(p) for p in err["loc"][1:]), "issue": err["msg"]} for err in e.errors()]
        return error_response(request, 400, "invalid_request_error", "invalid_body", "The request body is invalid.", fields=fields)

    @app.exception_handler(HTTPException)
    async def http_error(request, e: HTTPException):
        return error_response(request, e.status_code, "invalid_request_error", "http_error", str(e.detail))

    @app.exception_handler(Exception)
    async def unexpected_error(request, e: Exception):
        logger.exception("[%s] unexpected error", request.state.request_id)
        return error_response(request, 500, "api_error", "internal_error", "An internal error occurred.")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, body: ChatRequest):
        scheme, _, key = (request.headers.get("authorization") or "").partition(" ")
        tenant = hash_key(key)
        if scheme.lower() != "bearer" or tenant not in tenants:
            raise GatewayError(401, "authentication_error", "invalid_api_key", "A valid API key is required.")
        if body.stream:
            raise GatewayError(400, "invalid_request_error", "streaming_not_supported", "stream=true is not supported.")

        payload = body.model_dump(exclude_none=True)
        tokens = estimate_tokens(payload)
        if tokens > limiter.max_tokens:
            raise GatewayError(400, "invalid_request_error", "request_too_large",
                               f"This request needs about {tokens} tokens; the limit is {limiter.max_tokens} per minute.")
        try:
            row_id = await limiter.reserve(tenant, tokens)
        except RateLimited as e:
            seconds = max(1, math.ceil(e.retry_after))
            raise GatewayError(429, "rate_limit_error", "rate_limit_exceeded",
                               f"Token rate limit exceeded. Retry after {seconds} seconds.", {"Retry-After": str(seconds)}) from None

        try:
            data, used_fallback = await router.complete(payload, request.state.request_id)
        except UpstreamError as e:
            await limiter.release(row_id)  # a failed request doesn't use up the tenant's budget
            if e.retryable:
                raise GatewayError(503, "api_error", "upstream_unavailable",
                                   "The model service is temporarily unavailable. Please retry.", {"Retry-After": "5"}) from None
            raise GatewayError(400, "invalid_request_error", "upstream_rejected_request", "The model provider rejected the request.") from None
        except BaseException:
            await limiter.release(row_id)
            raise

        # swap the estimate for the actual usage
        actual = data.get("usage", {}).get("total_tokens")
        if isinstance(actual, int):
            await limiter.update(row_id, actual)
        return JSONResponse(data, headers={"X-Gateway-Fallback": str(used_fallback).lower()})

    return app
