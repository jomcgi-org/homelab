from pathlib import Path

import pytest

from knowledge.tools.gen_posts_manifest import build_manifest, make_slug


def write_post(root: Path, name: str, frontmatter: str, body: str = "Body.\n") -> str:
    posts = root / "docs" / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    (posts / name).write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return f"docs/posts/{name}"


def public_frontmatter(
    *, title: str = "Example", date: str = "2026-01-15", summary: str = "One sentence."
) -> str:
    return f"title: \"{title}\"\ndate: {date}\nsummary: '{summary}'\npublic: true\n"


def test_public_true_is_included_and_frontmatter_is_stripped(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(),
        "# Body\n\nPublished text.\n",
    )

    entries = build_manifest(tmp_path, [path])

    assert entries == [
        {
            "path": path,
            "slug": "example",
            "title": "Example",
            "date": "2026-01-15",
            "summary": "One sentence.",
            "content": "# Body\n\nPublished text.\n",
        }
    ]
    assert "frontmatter" not in entries[0]["content"]
    assert "public: true" not in entries[0]["content"]


@pytest.mark.parametrize(
    "frontmatter",
    [
        "title: Draft\ndate: 2026-01-15\nsummary: One sentence.\npublic: false\n",
        "title: Draft\ndate: 2026-01-15\nsummary: One sentence.\n",
    ],
)
def test_non_public_posts_are_excluded(tmp_path: Path, frontmatter: str):
    path = write_post(tmp_path, "2026-01-15-draft.md", frontmatter)
    assert build_manifest(tmp_path, [path]) == []


def test_readme_is_excluded(tmp_path: Path):
    posts = tmp_path / "docs" / "posts"
    posts.mkdir(parents=True)
    (posts / "README.md").write_text(
        f"---\n{public_frontmatter()}---\nInternal.\n", encoding="utf-8"
    )
    assert build_manifest(tmp_path, ["docs/posts/README.md"]) == []


def test_malformed_frontmatter_on_public_post_raises(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-broken.md",
        "title: 'Unclosed\ndate: 2026-01-15\nsummary: One sentence.\npublic: true\n",
    )
    with pytest.raises(ValueError, match="docs/posts/2026-01-15-broken.md"):
        build_manifest(tmp_path, [path])


def test_slug_strips_date_prefix():
    assert make_slug("docs/posts/2026-01-15-example.md") == "example"


def test_entries_sort_newest_first_then_slug(tmp_path: Path):
    paths = [
        write_post(
            tmp_path,
            "2025-12-31-old.md",
            public_frontmatter(title="Old", date="2025-12-31"),
        ),
        write_post(
            tmp_path,
            "2026-01-15-zebra.md",
            public_frontmatter(title="Zebra", date="2026-01-15"),
        ),
        write_post(
            tmp_path,
            "2026-01-15-alpha.md",
            public_frontmatter(title="Alpha", date="2026-01-15"),
        ),
    ]

    entries = build_manifest(tmp_path, paths)

    assert [entry["slug"] for entry in entries] == ["alpha", "zebra", "old"]
