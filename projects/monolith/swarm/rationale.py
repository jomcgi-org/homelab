"""Display-only parsing for implementer rationale trailers."""

from __future__ import annotations

import re


_RATIONALE_TAIL_LINES = 12
_HEADER = re.compile(r"RATIONALE", re.IGNORECASE)
_AREA = re.compile(
    r"area\s*:\s*(?P<area>.+?)(?:\s+(?:·|-|–)\s*why\s*:\s*(?P<why>.*))?$",
    re.IGNORECASE,
)
_DEVIATION = re.compile(r"deviation\s*:\s*(?P<deviation>.+)$", re.IGNORECASE)


def _empty(status: str, raw: str | None = None) -> dict:
    return {
        "raw": raw,
        "parse_status": status,
        "areas": [],
        "deviations": [],
        "parser_version": 1,
    }


def _decorated(line: str) -> str:
    return line.strip().strip("*#>\u2013- `\t").strip()


def parse_rationale(text: str | None) -> dict:
    """Parse the bounded rationale trailer, failing closed on ambiguity."""
    try:
        if not text:
            return _empty("none")
        lines = text.splitlines()
        nonempty = [index for index, line in enumerate(lines) if line.strip()]
        tail = set(nonempty[-_RATIONALE_TAIL_LINES:])
        headers = [
            index
            for index in nonempty
            if index in tail and _HEADER.fullmatch(_decorated(lines[index]))
        ]
        if not headers:
            return _empty("none")
        if len(headers) != 1:
            return _empty("unparseable", "\n".join(lines[headers[0] :]))

        header = headers[0]
        raw = "\n".join(lines[header:])
        areas = []
        deviations = []
        meaningful = 0
        for line in lines[header + 1 :]:
            stripped = _decorated(line)
            if not stripped:
                continue
            bullet = stripped.lstrip("-* \t")
            area = _AREA.fullmatch(bullet)
            deviation = _DEVIATION.fullmatch(bullet)
            if area:
                meaningful += 1
                areas.append(
                    {
                        "area": area.group("area").strip(),
                        "why": (area.group("why") or "").strip(),
                    }
                )
            elif deviation:
                meaningful += 1
                deviations.append(deviation.group("deviation").strip())
            elif stripped in ("```", "---"):
                continue
            else:
                return _empty("unparseable", raw)
        if not meaningful:
            return _empty("unparseable", raw)
        return {
            "raw": raw,
            "parse_status": "parsed",
            "areas": areas,
            "deviations": deviations,
            "parser_version": 1,
        }
    except Exception:
        return _empty("unparseable")
