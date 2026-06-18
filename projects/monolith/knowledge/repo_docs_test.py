import json
from pathlib import Path

from knowledge.repo_docs import ManifestEntry, load_manifest


def test_load_manifest_parses_ndjson(tmp_path: Path):
    p = tmp_path / "m.ndjson"
    p.write_text(
        "\n".join(
            json.dumps(o, sort_keys=True)
            for o in [
                {"path": "docs/a.md", "sha256": "h1", "title": "A", "content": "# A"},
                {
                    "path": "CLAUDE.md",
                    "sha256": "h2",
                    "title": "Root",
                    "content": "# Root",
                },
            ]
        )
        + "\n"
    )
    entries = load_manifest(p)
    assert [e.path for e in entries] == ["docs/a.md", "CLAUDE.md"]
    assert entries[0] == ManifestEntry(
        path="docs/a.md", sha256="h1", title="A", content="# A"
    )


def test_load_manifest_missing_file_returns_empty(tmp_path: Path):
    assert load_manifest(tmp_path / "nope.ndjson") == []
