"""Redacts an OpenAI-style SSE completion stream as it passes through.

Two buffers, both small:
  - bytes, until a complete SSE event has arrived (network chunks don't line up with events)
  - text, per choice, inside StreamRedactor (only the current word is held)
"""

import json
import logging

from .redactor import StreamRedactor

logger = logging.getLogger(__name__)

MAX_EVENT_BYTES = 1_000_000


def split_events(buffer: bytes) -> tuple[list[str], bytes]:
    """Takes complete SSE events off the front of the buffer. Returns (event data, leftover bytes)."""
    buffer = buffer.replace(b"\r\n", b"\n")
    events = []
    while b"\n\n" in buffer:
        raw, buffer = buffer.split(b"\n\n", 1)
        # Decode whole events only, so a multi-byte character split across chunks is safe.
        lines = [line[5:].removeprefix(" ") for line in raw.decode().split("\n") if line.startswith("data:")]
        if lines:
            events.append("\n".join(lines))
    return events, buffer


def sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


async def redact_stream(upstream_chunks):
    buffer = b""
    redactors: dict[int, StreamRedactor] = {}  # one per choice index
    last_event: dict = {}

    def flush_all():
        # Send whatever is still held, as one extra chunk per choice.
        for index, redactor in redactors.items():
            if tail := redactor.flush():
                fields = {k: last_event[k] for k in ("id", "object", "created", "model") if k in last_event}
                yield sse(json.dumps({**fields, "choices": [{"index": index, "delta": {"content": tail}, "finish_reason": None}]}))
        redactors.clear()

    try:
        async for chunk in upstream_chunks:
            events, buffer = split_events(buffer + chunk)
            if len(buffer) > MAX_EVENT_BYTES:
                raise ValueError("SSE event too large")

            for data in events:
                if data == "[DONE]":
                    for out in flush_all():
                        yield out
                    yield sse("[DONE]")
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    logger.warning("Dropping an unparseable upstream event")  # can't inspect it, so don't forward it
                    continue

                for choice in event.get("choices", []):
                    index = choice.get("index", 0)
                    redactor = redactors.setdefault(index, StreamRedactor())
                    delta = choice.setdefault("delta", {})
                    if isinstance(delta.get("content"), str):
                        delta["content"] = redactor.feed(delta["content"])
                    if choice.get("finish_reason"):
                        delta["content"] = (delta.get("content") or "") + redactor.flush()
                        del redactors[index]
                last_event = event
                yield sse(json.dumps(event))
    except Exception:
        logger.exception("Upstream stream failed")
        # Don't flush: the held text might be the first half of an SSN.
        yield sse(json.dumps({"error": {"type": "upstream_error", "message": "The upstream stream ended unexpectedly."}}))
        return

    # Upstream closed without [DONE]: whatever is held is final now.
    for out in flush_all():
        yield out
