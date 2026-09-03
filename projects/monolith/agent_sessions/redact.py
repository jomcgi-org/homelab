"""Secret redaction for session transcript exports."""

from __future__ import annotations

import re


_REDACTIONS = (
    re.compile(r"\b(?:sk|sk-proj)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?im)\b(authorization\s*:\s*bearer\s+)[^\s]+",
    ),
    re.compile(
        r"(?im)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"\s*[:=]\s*)['\"]?[^\s'\"]+",
    ),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def redact_text(text: str) -> tuple[str, int]:
    """Replace common credential forms and return the replacement count."""
    redacted = text
    count = 0
    for pattern in _REDACTIONS:
        if pattern.groups:
            redacted, replacements = pattern.subn(r"\1[REDACTED]", redacted)
        else:
            redacted, replacements = pattern.subn("[REDACTED]", redacted)
        count += replacements
    return redacted, count
