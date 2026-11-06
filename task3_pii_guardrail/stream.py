"""Redacts an OpenAI-style SSE stream on the way through.

There are two buffers: raw bytes until a full SSE event has arrived (network
chunks don't line up with events), and the text buffer in each StreamRedactor.
"""

import json
import logging

from .redactor import StreamRedactor

logger = logging.getLogger(__name__)

MAX_EVENT_BYTES = 1_000_000


def split_events(buffer: bytes) -> tuple[list[str], bytes]:
    """Pulls complete events off the front of the buffer -> (list of event data, leftover bytes)."""
    buffer = buffer.replace(b"\r\n", b"\n")
    events = []
    while b"\n\n" in buffer:
        raw, buffer = buffer.split(b"\n\n", 1)
        # only decode complete events, so a utf-8 char split across two chunks is fine
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
        # emit anything still buffered as an extra chunk per choice
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
        # don't flush here, the buffer might be the first half of an SSN
        yield sse(json.dumps({"error": {"type": "upstream_error", "message": "The upstream stream ended unexpectedly."}}))
        return

    # upstream closed without sending [DONE]
    for out in flush_all():
        yield out
