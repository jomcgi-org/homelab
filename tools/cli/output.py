"""Token-efficient output formatting for Claude Code consumption."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

TMPDIR = Path("/tmp/homelab-cli/notes")


def compact_line(
    id: int,
    path: str,
    source: str,
    error: str | None = None,
    retry_count: int = 0,
) -> str:
    """One-line summary of a dead-lettered raw."""
    base = f"[{id}] {path} ({source})"
    if error:
        return f"{base} — {error} [{retry_count} retries]"
    return base


def search_line(
    score: float,
    note_id: str,
    title: str,
    note_type: str,
    edges: list[dict],
) -> str:
    """One-line summary of a search result with optional edge line."""
    line = f"[{score:.2f}] {note_id} — {title} ({note_type})"
    edge_str = format_edges(edges)
    if edge_str:
        line += f"\n  {edge_str}"
    return line


def format_edges(edges: list[dict]) -> str:
    """Compact edge representation: type→target, type→target."""
    typed = [e for e in edges if e.get("kind") == "edge"]
    if not typed:
        return ""
    return ", ".join(f"{e['edge_type']}→{e['target_id']}" for e in typed)


def task_line(
    note_id: str,
    title: str,
    status: str,
    size: str | None = None,
    due: str | None = None,
    blocked_by: list[str] | None = None,
) -> str:
    """One-line summary of a task."""
    parts = []
    if size:
        parts.append(size)
    if due:
        parts.append(f"due {due}")
    detail = f" ({', '.join(parts)})" if parts else ""
    line = f"[{status}]  {note_id} — {title}{detail}"
    if blocked_by:
        blockers = ", ".join(f"blocked-by→{b}" for b in blocked_by)
        line += f"\n  {blockers}"
    return line


def scheduler_line(
    name: str,
    interval_secs: int,
    next_run_at: str,
    last_run_at: str | None,
    last_status: str | None,
) -> str:
    """One-line summary of a scheduled job."""
    next_short = _short_time(next_run_at)
    last = "never run"
    if last_run_at:
        last_short = _short_time(last_run_at)
        status = last_status or "unknown"
        last = f"last {status} at {last_short}"
    return f"{name:<32} every {interval_secs:>5}s  next {next_short}  {last}"


def _short_time(iso: str) -> str:
    """Render an ISO-8601 timestamp as HH:MM, falling back to the input on parse error."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return iso
    return dt.strftime("%H:%M")


def write_to_tmpfile(name: str, content: str) -> Path:
    """Write content to a tmpfile and return the path."""
    TMPDIR.mkdir(parents=True, exist_ok=True)
    path = TMPDIR / f"{name}.md"
    path.write_text(content)
    return path
