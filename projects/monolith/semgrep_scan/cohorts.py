"""Diff cohort extraction: the shape of a scanned PR diff (changed-file count,
changed lines, per-language breakdown) for the perf comparison's per-cohort
speedup segmentation.

Shared by the live webhook (from the ``/pulls/{n}/files`` stats it already
fetches) and the GitHub-API backfill, so both compute the identical numbers from
the identical source. ``changed_lines`` is ``additions + deletions`` (the diff
size) rather than "lines in file", precisely because that is what both paths get
straight from the ``/pulls/{n}/files`` endpoint.
"""

from __future__ import annotations

# Scannable extension -> language label (the fc-invoke semgrep guest's rule
# languages). Keep in sync with router._SCANNABLE_EXTS.
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".jsx": "javascript",
    ".js": "javascript",
    ".tsx": "typescript",
    ".ts": "typescript",
    ".rs": "rust",
}


def language_for(path: str) -> str | None:
    """The scan language for ``path`` by extension, or None if unscannable.

    Longer extensions are checked first (``.jsx`` before ``.js``) so a ``.jsx``
    file is not mislabeled by the ``.js`` suffix match.
    """
    for ext, lang in _EXT_LANG.items():
        if path.endswith(ext):
            return lang
    return None


def diff_cohort(entries: list[dict]) -> dict:
    """Cohort metadata from GitHub ``/pulls/{n}/files`` entries.

    Each entry is a file dict with ``filename``, ``status`` and the diff stats
    ``additions``/``deletions``. Only scannable, non-removed files count (the set
    actually scanned). Returns ``{file_count, changed_lines, languages}`` where
    ``languages`` maps language -> changed lines.
    """
    file_count = 0
    changed_lines = 0
    languages: dict[str, int] = {}
    for entry in entries:
        if entry.get("status") == "removed":
            continue
        lang = language_for(entry.get("filename", ""))
        if lang is None:
            continue
        lines = int(entry.get("additions", 0) or 0) + int(
            entry.get("deletions", 0) or 0
        )
        file_count += 1
        changed_lines += lines
        languages[lang] = languages.get(lang, 0) + lines
    return {
        "file_count": file_count,
        "changed_lines": changed_lines,
        "languages": languages,
    }
