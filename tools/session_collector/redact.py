"""Local transcript redaction."""

from __future__ import annotations

import re
from collections import Counter

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "aws_secret",
        re.compile(
            r"(?i)(?:aws_secret_access_key|SecretAccessKey)\s*[=:]\s*[\"']?"
            r"[A-Za-z0-9+/=]{40}"
        ),
    ),
    (
        "github_token",
        re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}")),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    (
        "pem_block",
        re.compile(
            r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?"
            r"-----END \1-----",
            re.DOTALL,
        ),
    ),
    (
        "bearer_header",
        re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
    ("basic_auth_url", re.compile(r":\/\/[^/\s:]+:[^@\s]+@")),
    (
        "kv_secret",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
            r"\s*[=:]\s*[\"']?[^\s\"']{8,}"
        ),
    ),
    ("cf_cookie", re.compile(r"CF_Authorization=[A-Za-z0-9._-]{20,}")),
    ("onepassword", re.compile(r"ops_[A-Za-z0-9]{20,}")),
)

ENV_LINE = re.compile(r"^[A-Z_]{3,}=", re.MULTILINE)


class Redactor:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter(
            {name: 0 for name, _ in PATTERNS} | {"env_dump": 0}
        )

    def text(self, value: str) -> str:
        for name, pattern in PATTERNS:
            value, count = pattern.subn(f"[REDACTED:{name}]", value)
            self.counts[name] += count
        return value

    def tool_result(self, value: str) -> str:
        if len(ENV_LINE.findall(value[:200])) >= 3:
            self.counts["env_dump"] += 1
            return "[REDACTED:env_dump]"
        return self.text(value)
