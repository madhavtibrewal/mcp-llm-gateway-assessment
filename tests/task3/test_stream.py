import asyncio
import json
import random

import httpx

from task3_pii_guardrail.redactor import REDACTED, redact
from task3_pii_guardrail.stream import redact_stream, split_events

TEXT = "Hi! Email john.doe@example.com, SSN 123-45-6789, card 4111 1111 1111 1111. Bye."


def event(content=None, finish=None, index=0) -> bytes:
    delta = {} if content is None else {"content": content}
    chunk = {"id": "c1", "choices": [{"index": index, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(chunk)}\n\n".encode()


async def chunks(items):
    for item in items:
        yield item


async def run(items) -> list[str]:
    output = b"".join([piece async for piece in redact_stream(chunks(items))])
    return split_events(output)[0]


def content_of(events: list[str]) -> str:
    return "".join(
        choice["delta"].get("content") or ""
        for data in events if data != "[DONE]"
        for choice in json.loads(data).get("choices", [])
    )


async def test_pii_split_across_deltas_and_network_chunks_is_redacted():
    rng = random.Random(1)
    for _ in range(30):
        # Random 1-5 char deltas, then the byte stream re-cut at random points.
        frames, i = [], 0
        while i < len(TEXT):
            n = rng.randint(1, 5)
            frames.append(event(TEXT[i:i + n]))
            i += n
        blob = b"".join(frames + [event(finish="stop"), b"data: [DONE]\n\n"])
        pieces, j = [], 0
        while j < len(blob):
            k = rng.randint(1, 40)
            pieces.append(blob[j:j + k])
            j += k

        events = await run(pieces)
        assert content_of(events) == redact(TEXT)
        assert events[-1] == "[DONE]"


async def test_held_text_is_sent_with_the_finish_event():
    events = await run([event("mail john"), event("@example.com"), event(finish="stop"), b"data: [DONE]\n\n"])
    assert json.loads(events[-2])["choices"][0]["delta"]["content"] == REDACTED


async def test_choices_are_redacted_separately():
    items = [event("a: john", index=0), event("b: 078 05", index=1), event("@example.com", index=0),
             event(" 1120", index=1), b"data: [DONE]\n\n"]
    events = await run(items)
    by_choice = {}
    for data in events[:-1]:
        for choice in json.loads(data)["choices"]:
            by_choice[choice["index"]] = by_choice.get(choice["index"], "") + (choice["delta"].get("content") or "")
    assert by_choice == {0: f"a: {REDACTED}", 1: f"b: {REDACTED}"}


async def test_first_words_are_sent_before_the_rest_arrives():
    upstream_may_continue = asyncio.Event()

    async def upstream():
        yield event("Hello there, how are you? ")
        await upstream_may_continue.wait()  # stalls until we've seen output
        yield b"data: [DONE]\n\n"

    output = redact_stream(upstream())
    first = await asyncio.wait_for(output.__anext__(), timeout=1)
    assert "Hello there, how are you? " in first.decode()
    upstream_may_continue.set()
    assert [piece async for piece in output][-1] == b"data: [DONE]\n\n"


async def test_upstream_failure_drops_held_text():
    async def upstream():
        yield event("my ssn is 123-45-67")
        raise httpx.ReadError("connection reset")

    output = b"".join([piece async for piece in redact_stream(upstream())]).decode()
    assert "123-45-67" not in output
    assert "upstream_error" in output


async def test_unparseable_events_are_dropped():
    events = await run([b"data: {broken json a@b.com\n\n", event("ok", finish="stop"), b"data: [DONE]\n\n"])
    assert "a@b.com" not in "".join(events)
    assert content_of(events) == "ok"
