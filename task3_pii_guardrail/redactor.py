"""PII regexes, plus redaction for whole strings and for streamed text."""

import re

REDACTED = "[REDACTED]"

EMAIL = r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,63}"
# SSN with dashes or spaces. 000/666/9xx areas, 00 groups and 0000 serials aren't valid SSNs.
SSN = r"(?<![0-9-])(?!000|666|9[0-9][0-9])[0-9]{3}(?P<sep>[- ])(?!00)[0-9]{2}(?P=sep)(?!0000)[0-9]{4}(?![0-9-])"
# 13-19 digits, optionally grouped with spaces or dashes (Luhn check is below)
CARD = r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])"

PII = re.compile(f"(?P<email>{EMAIL})|(?P<ssn>{SSN})|(?P<card>{CARD})")

# bigger than any match can be. Only matters when there's no whitespace to cut at.
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
    # fails Luhn -> probably an order number or similar, leave it
    if match.group("card") and not luhn_valid(match.group()):
        return match.group()
    return REDACTED


def redact(text: str) -> str:
    return PII.sub(_replace, text)


class StreamRedactor:
    """Redacts streamed text while holding back as little as possible.

    Emails can't contain spaces, and cards/SSNs only have spaces between two
    digits. So whitespace that isn't digit-space-digit can't be inside a match,
    and everything before the last such whitespace is safe to redact and send.
    Usually that means only the current word is held back.
    """

    def __init__(self):
        self.buffer = ""

    def feed(self, text: str) -> str:
        self.buffer += text
        cut = self._safe_cut()
        ready, self.buffer = self.buffer[:cut], self.buffer[cut:]
        return redact(ready)

    def flush(self) -> str:
        """End of stream: redact and return whatever is left."""
        ready, self.buffer = self.buffer, ""
        return redact(ready)

    def discard(self):
        """Upstream died, so drop the buffer (it could be half an SSN)."""
        self.buffer = ""

    def _safe_cut(self) -> int:
        buf = self.buffer
        for i in range(len(buf) - 1, -1, -1):
            if buf[i].isspace():
                digit_before = i > 0 and buf[i - 1].isdigit()
                digit_after = i + 1 == len(buf) or buf[i + 1].isdigit()  # next char hasn't arrived yet
                if not (digit_before and digit_after):
                    return i + 1
        if len(buf) <= MAX_HOLD:
            return 0
        # no usable whitespace for ages (base64 blob etc). Cut anyway so the buffer
        # can't grow forever, just not in the middle of a match.
        cut = len(buf) - MAX_HOLD
        for m in PII.finditer(buf):
            if m.start() < cut < m.end():
                return m.start()
        return cut
