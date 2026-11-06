"""PII patterns, and redaction for whole strings and for streamed text."""

import re

REDACTED = "[REDACTED]"

EMAIL = r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,63}"
# US SSN with a consistent "-" or " " separator. Area 000/666/9xx, group 00 and serial 0000 are never issued.
SSN = r"(?<![0-9-])(?!000|666|9[0-9][0-9])[0-9]{3}(?P<sep>[- ])(?!00)[0-9]{2}(?P=sep)(?!0000)[0-9]{4}(?![0-9-])"
# 13-19 digits with optional single space/dash separators. Luhn-checked below.
CARD = r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])"

PII = re.compile(f"(?P<email>{EMAIL})|(?P<ssn>{SSN})|(?P<card>{CARD})")

# Longer than any possible match. Only used when a stream has no safe place to cut.
MAX_HOLD = 1024


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _replace(match: re.Match) -> str:
    # A digit run that fails the Luhn check is an order number or similar, not a card.
    if match.group("card") and not luhn_valid(match.group()):
        return match.group()
    return REDACTED


def redact(text: str) -> str:
    return PII.sub(_replace, text)


class StreamRedactor:
    """Redacts a text stream piece by piece while holding back as little as possible.

    The idea: emails contain no spaces, and card numbers and SSNs contain spaces
    only *between two digits*. So any other whitespace is a point that no PII
    match can cross. Everything up to the last such point can be redacted and
    sent now; only the text after it (usually the current word) is held back.
    """

    def __init__(self):
        self.buffer = ""

    def feed(self, text: str) -> str:
        self.buffer += text
        cut = self._safe_cut()
        ready, self.buffer = self.buffer[:cut], self.buffer[cut:]
        return redact(ready)

    def flush(self) -> str:
        """End of the stream: everything left is final."""
        ready, self.buffer = self.buffer, ""
        return redact(ready)

    def discard(self):
        """The stream failed: drop held text, it might be half an SSN."""
        self.buffer = ""

    def _safe_cut(self) -> int:
        buf = self.buffer
        for i in range(len(buf) - 1, -1, -1):
            if buf[i].isspace():
                digit_before = i > 0 and buf[i - 1].isdigit()
                digit_after = i + 1 == len(buf) or buf[i + 1].isdigit()  # not arrived yet: assume the worst
                if not (digit_before and digit_after):
                    return i + 1
        if len(buf) <= MAX_HOLD:
            return 0
        # A long run with no safe whitespace (e.g. a base64 blob): cut anyway to
        # keep memory bounded, but never in the middle of a match.
        cut = len(buf) - MAX_HOLD
        for m in PII.finditer(buf):
            if m.start() < cut < m.end():
                return m.start()
        return cut
