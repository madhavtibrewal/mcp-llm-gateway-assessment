import random

import pytest

from task3_pii_guardrail.redactor import MAX_HOLD, REDACTED, StreamRedactor, redact

SAMPLES = ["john.doe@example.com", "a@mail.co.uk", "123-45-6789", "078 05 1120", "4111 1111 1111 1111", "5555-5555-5555-4444"]


@pytest.mark.parametrize("pii", SAMPLES)
def test_redacts_pii(pii):
    assert redact(f"my details: {pii}.") == f"my details: {REDACTED}."


@pytest.mark.parametrize(
    "text",
    [
        "order 1234567890123",  # long number that fails the Luhn check
        "4111 1111 1111 1112",  # one digit off a valid card
        "ssn 666-12-3456",  # never-issued area number
        "call 415-555-0132",  # phone number
        "on 2024-01-15 12:30",  # date and time
        "john@ is not an address",
    ],
)
def test_leaves_lookalikes_alone(text):
    assert redact(text) == text


def stream(text: str, cuts: list[int]) -> list[str]:
    redactor, pieces, prev = StreamRedactor(), [], 0
    for cut in sorted(set(cuts)) + [len(text)]:
        pieces.append(redactor.feed(text[prev:cut]))
        prev = cut
    pieces.append(redactor.flush())
    return pieces


@pytest.mark.parametrize("pii", SAMPLES)
def test_pii_split_at_any_point_is_still_redacted(pii):
    text = f"before {pii} after"
    for cut in range(1, len(text)):
        pieces = stream(text, [cut])
        assert "".join(pieces) == f"before {REDACTED} after"
        assert not any(pii in piece for piece in pieces)


def test_random_chunking_matches_one_shot_redaction():
    rng = random.Random(42)
    words = ["hello", "the", "ok.", "order", "#1234567890123", "a@b.co", *SAMPLES]
    for _ in range(300):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 30)))
        cuts = [rng.randint(1, len(text)) for _ in range(rng.randint(0, 20))]
        assert "".join(stream(text, cuts)) == redact(text)


def test_plain_text_is_not_held_back():
    r = StreamRedactor()
    assert r.feed("Hello there, ") == "Hello there, "
    assert r.feed("how are yo") == "how are "  # only the unfinished word waits
    assert r.feed("u?") == ""
    assert r.flush() == "you?"


def test_digits_separated_by_spaces_are_held_until_the_number_ends():
    r = StreamRedactor()
    assert r.feed("card 4111 1111 ") == "card "
    assert r.feed("1111 1111 thanks") == f"{REDACTED} "


def test_memory_stays_bounded_without_any_whitespace():
    r = StreamRedactor()
    for _ in range(10_000):
        r.feed("abcdefghij")
        assert len(r.buffer) <= MAX_HOLD + 10
