"""Browser artifact contract. Retrieval is injected; this module grants no access."""

import base64
import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.parse import urlsplit

DEFAULT_VIEWPORTS = ((1440, 900), (390, 844))
CONSOLE_MAX_ENTRIES = 200
CONSOLE_MAX_BYTES = 65536
NETWORK_MAX_ENTRIES = 200
NETWORK_MAX_BYTES = 65536
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
KINDS = frozenset(
    (
        "screenshot",
        "trace",
        "console_log",
        "network_log",
        "repro_steps",
        "interaction_outcome",
        "accessibility_snapshot",
    )
)


def utc_time(value: str) -> datetime:
    """Accept RFC3339 UTC, including fractional seconds and the +00:00 form."""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value
    ):
        raise ValueError("timestamp must be RFC3339 UTC")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def positive_viewport(viewport) -> bool:
    return (
        isinstance(viewport, (tuple, list))
        and len(viewport) == 2
        and all(type(n) is int and n > 0 for n in viewport)
    )


def preview_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class EvidenceRecord:
    kind: str
    source_url: str
    app_commit: str
    task_id: str
    run_id: str
    session_id: str
    capture_time: str
    media_type: str
    sha256: str
    size_bytes: int
    storage: dict[str, str]
    expires_at: str
    link: str
    upload_status: str
    viewport: tuple[int, int] | None = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError("unknown evidence kind")
        if not re.fullmatch(r"[0-9a-f]{40}", self.app_commit):
            raise ValueError("app_commit must be 40 lowercase hex characters")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        if not preview_url(self.source_url):
            raise ValueError("source_url must be an HTTP(S) URL without userinfo")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.task_id,
                self.run_id,
                self.session_id,
                self.media_type,
                self.link,
            )
        ):
            raise ValueError("artifact identity, media_type and link are required")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a nonnegative integer")
        if not isinstance(self.storage, dict) or set(self.storage) != {
            "backend",
            "uri",
        }:
            raise ValueError("storage requires backend and uri")
        if any(not isinstance(v, str) or not v for v in self.storage.values()):
            raise ValueError("storage fields must be nonempty strings")
        if self.upload_status not in ("uploaded", "failed", "pending"):
            raise ValueError("unknown upload_status")
        capture, expiry = utc_time(self.capture_time), utc_time(self.expires_at)
        if expiry <= capture:
            raise ValueError("expiry must follow capture")
        if self.viewport is not None and not positive_viewport(self.viewport):
            raise ValueError("viewport must contain positive integer dimensions")
        if self.kind == "screenshot" and self.viewport is None:
            raise ValueError("screenshots require a viewport")


@dataclass(frozen=True)
class EvidenceRequirements:
    viewports: tuple[tuple[int, int], ...] = DEFAULT_VIEWPORTS
    functional_checks: tuple[str, ...] = ("functional",)
    accessibility_checks: tuple[str, ...] = ("accessibility",)

    def __post_init__(self):
        if not self.viewports or any(not positive_viewport(v) for v in self.viewports):
            raise ValueError("required viewports must be nonempty and valid")
        if len({tuple(v) for v in self.viewports}) != len(self.viewports):
            raise ValueError("duplicate required viewport")
        for checks in (self.functional_checks, self.accessibility_checks):
            if not checks or any(not isinstance(c, str) or not c for c in checks):
                raise ValueError("required check names must be nonempty")
            if len(set(checks)) != len(checks):
                raise ValueError("duplicate required check")


@dataclass(frozen=True)
class EvidenceVerdict:
    status: str
    reasons: tuple[dict, ...] = ()

    def __post_init__(self):
        if self.status not in ("complete", "incomplete"):
            raise ValueError("unknown evidence verdict")
        if self.status == "complete" and self.reasons:
            raise ValueError("a complete verdict cannot have failure reasons")


FetchBytes = Callable[[EvidenceRecord], bytes | None]


def viewport_key(viewport) -> str:
    return f"{viewport[0]}x{viewport[1]}"


def _png_dimensions(data):
    """Return (valid, (width, height) or None) after walking every chunk.

    Each chunk length and CRC32 is verified. IHDR must be first with a
    13-byte payload, at least one IDAT must appear, and a zero-length IEND
    must be last with no trailing bytes.
    """
    if len(data) < 8 or data[:8] != PNG_SIGNATURE:
        return False, None
    offset = 8
    total = len(data)
    first = True
    seen_idat = False
    dimensions = None
    while True:
        if offset + 8 > total:
            return False, None
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunk_type = data[offset + 4 : offset + 8]
        if offset + 12 + length > total:
            return False, None
        chunk_data = data[offset + 8 : offset + 8 + length]
        (stored_crc,) = struct.unpack(
            ">I", data[offset + 8 + length : offset + 12 + length]
        )
        if (
            zlib.crc32(data[offset + 4 : offset + 8 + length]) & 0xFFFFFFFF
            != stored_crc
        ):
            return False, None
        if first:
            if chunk_type != b"IHDR" or length != 13:
                return False, None
            dimensions = struct.unpack(">II", chunk_data[:8])
            first = False
        else:
            if chunk_type == b"IHDR":
                return False, None
            if chunk_type == b"IDAT":
                seen_idat = True
            if chunk_type == b"IEND":
                if length != 0:
                    return False, None
                if offset + 12 + length != total:
                    return False, None
                if not seen_idat:
                    return False, None
                return True, dimensions
        if chunk_type != b"IEND":
            offset += 12 + length
            if offset >= total:
                return False, None
            continue
        return False, None


def _artifact_bytes(record, now, fetch_bytes):
    """Return verified bytes or stable reason codes, never a retrieval URL."""
    reasons = []
    if record.upload_status != "uploaded":
        reasons.append("upload_" + record.upload_status)
    if utc_time(record.expires_at) <= now:
        reasons.append("expired")
    if utc_time(record.capture_time) > now:
        reasons.append("capture_in_future")
    if reasons:
        return None, reasons
    try:
        data = fetch_bytes(record)
    except Exception:
        return None, ["fetch_failed"]
    if not isinstance(data, bytes):
        return None, ["artifact_missing"]
    if len(data) != record.size_bytes:
        reasons.append("size_mismatch")
    if hashlib.sha256(data).hexdigest() != record.sha256:
        reasons.append("hash_mismatch")
    if record.kind == "screenshot":
        if record.media_type != "image/png":
            reasons.append("invalid_png")
        else:
            valid, dimensions = _png_dimensions(data)
            if not valid:
                reasons.append("invalid_png")
            elif dimensions != tuple(record.viewport):
                reasons.append("png_dimensions_mismatch")
    return (None if reasons else data), reasons


def verify_evidence(
    records: Iterable[EvidenceRecord],
    required: EvidenceRequirements | None,
    now: datetime,
    fetch_bytes: FetchBytes,
) -> EvidenceVerdict:
    """Verify artifacts and the single retrieved interaction-outcome document.

    Outcome JSON has navigation="passed" and viewports keyed as "1440x900".
    Each viewport contains functional and accessibility maps of check name to
    status. Only "passed" succeeds. Missing, skipped, errored and failed checks
    are independently reported. No accessibility artifact supplies a screenshot.
    """
    required = required or EvidenceRequirements()
    records = tuple(records)
    reasons = []
    screenshots = set()
    outcomes = []
    identities = set()
    if now.tzinfo is None or now.utcoffset() is None:
        return EvidenceVerdict("incomplete", ({"code": "invalid_now"},))
    for index, record in enumerate(records):
        if not isinstance(record, EvidenceRecord):
            reasons.append({"code": "invalid_record", "artifact": index})
            continue
        identities.add(
            (
                record.source_url,
                record.app_commit,
                record.task_id,
                record.run_id,
                record.session_id,
            )
        )
        data, errors = _artifact_bytes(record, now, fetch_bytes)
        reasons.extend({"code": code, "artifact": index} for code in errors)
        if data is None:
            continue
        if record.kind == "screenshot":
            screenshots.add(tuple(record.viewport))
        if record.kind == "interaction_outcome":
            try:
                outcome = json.loads(data)
                if not isinstance(outcome, dict):
                    raise ValueError("outcome must be an object")
                outcomes.append(outcome)
            except (ValueError, UnicodeError):
                reasons.append({"code": "invalid_outcome", "artifact": index})
    if len(identities) > 1:
        reasons.append({"code": "identity_mismatch"})
    for viewport in required.viewports:
        if tuple(viewport) not in screenshots:
            reasons.append(
                {"code": "screenshot_missing", "viewport": viewport_key(viewport)}
            )
    if len(outcomes) != 1:
        reasons.append(
            {"code": "outcome_missing" if not outcomes else "outcome_ambiguous"}
        )
    outcome = outcomes[0] if len(outcomes) == 1 else {}
    navigation = outcome.get("navigation")
    if navigation != "passed":
        reasons.append(
            {
                "code": "navigation_missing"
                if navigation is None
                else "navigation_failed"
            }
        )
    views = outcome.get("viewports", {})
    for viewport in required.viewports:
        key = viewport_key(viewport)
        view = views.get(key, {}) if isinstance(views, dict) else {}
        for category, checks in (
            ("functional", required.functional_checks),
            ("accessibility", required.accessibility_checks),
        ):
            statuses = view.get(category, {}) if isinstance(view, dict) else {}
            for check in checks:
                status = statuses.get(check) if isinstance(statuses, dict) else None
                if status != "passed":
                    code = (
                        {
                            None: "check_missing",
                            "skipped": "check_skipped",
                            "errored": "check_errored",
                            "failed": "check_failed",
                        }.get(status, "check_invalid")
                        if isinstance(status, (str, type(None)))
                        else "check_invalid"
                    )
                    reasons.append(
                        {
                            "code": code,
                            "viewport": key,
                            "category": category,
                            "check": check,
                        }
                    )
    return EvidenceVerdict("incomplete" if reasons else "complete", tuple(reasons))


@dataclass(frozen=True)
class BoundedCapture:
    entries: tuple[str, ...]
    size_bytes: int
    truncated: bool
    dropped_count: int


def _bounded_capture(entries, max_entries, max_bytes):
    kept, size, dropped = [], 0, 0
    for entry in entries:
        text = entry if isinstance(entry, str) else json.dumps(entry, sort_keys=True)
        entry_size = len(text.encode("utf-8"))
        if len(kept) >= max_entries or size + entry_size > max_bytes:
            dropped += 1
        else:
            kept.append(text)
            size += entry_size
    return BoundedCapture(tuple(kept), size, dropped > 0, dropped)


def capture_console(entries) -> BoundedCapture:
    return _bounded_capture(entries, CONSOLE_MAX_ENTRIES, CONSOLE_MAX_BYTES)


def capture_network(entries) -> BoundedCapture:
    return _bounded_capture(entries, NETWORK_MAX_ENTRIES, NETWORK_MAX_BYTES)


def image_blocks_for_review(
    records, fetch_bytes: FetchBytes, *, now=None
) -> list[dict]:
    """Return base64 image content. Raise if any screenshot cannot be verified."""
    now = now or datetime.now(timezone.utc)
    blocks = []
    for record in records:
        if record.kind != "screenshot":
            continue
        data, errors = _artifact_bytes(record, now, fetch_bytes)
        if errors:
            raise ValueError("incomplete image evidence: " + ", ".join(errors))
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
        )
    if not blocks:
        raise ValueError("incomplete image evidence: screenshot_missing")
    return blocks


def bounded_summary(records, verdict: EvidenceVerdict, max_chars: int) -> str:
    """Cap characters, with verdict/reasons first and artifact metadata after."""
    if type(max_chars) is not int or max_chars < 0:
        raise ValueError("max_chars must be a nonnegative integer")
    lines = [verdict.status, json.dumps(verdict.reasons, sort_keys=True)]
    for record in records:
        view = viewport_key(record.viewport) if record.viewport else "none"
        lines.append(
            f"{record.kind} viewport={view} sha256={record.sha256[:12]} "
            f"size={record.size_bytes} expiry={record.expires_at} link={record.link}"
        )
    summary = "\n".join(lines)
    if len(summary) <= max_chars:
        return summary
    marker = "\n[truncated]"
    if max_chars < len(marker):
        return summary[:max_chars]
    return summary[: max_chars - len(marker)] + marker
