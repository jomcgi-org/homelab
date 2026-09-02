from pathlib import Path

import pytest

from knowledge.tools.gen_posts_manifest import build_manifest, make_slug


def write_post(root: Path, name: str, frontmatter: str, body: str = "Body.\n") -> str:
    posts = root / "docs" / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    (posts / name).write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return f"docs/posts/{name}"


def public_frontmatter(
    *,
    title: str = "Example",
    date: str = "2026-01-15",
    summary: str = "One sentence.",
    tags: str | None = None,
) -> str:
    tag_line = f"tags: {tags}\n" if tags is not None else ""
    return (
        f"title: \"{title}\"\ndate: {date}\nsummary: '{summary}'\n"
        f"{tag_line}public: true\n"
    )


def write_figure(root: Path, name: str, source: str) -> str:
    figures = root / "docs" / "posts" / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    (figures / name).write_text(source, encoding="utf-8")
    return f"docs/posts/figures/{name}"


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
            "tags": [],
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


def test_figures_readme_is_excluded(tmp_path: Path):
    figures = tmp_path / "docs" / "posts" / "figures"
    figures.mkdir(parents=True)
    (figures / "README.md").write_text(
        f"---\n{public_frontmatter()}---\nInternal.\n", encoding="utf-8"
    )
    assert build_manifest(tmp_path, ["docs/posts/figures/README.md"]) == []


def test_malformed_frontmatter_on_public_post_raises(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-broken.md",
        "title: 'Unclosed\ndate: 2026-01-15\nsummary: One sentence.\npublic: true\n",
    )
    with pytest.raises(ValueError, match="docs/posts/2026-01-15-broken.md"):
        build_manifest(tmp_path, [path])


def test_duplicate_public_post_slugs_raise(tmp_path: Path):
    first = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(date="2026-01-15"),
    )
    second = write_post(
        tmp_path,
        "2026-01-20-example.md",
        public_frontmatter(date="2026-01-20"),
    )

    with pytest.raises(ValueError) as exc_info:
        build_manifest(tmp_path, [first, second])

    message = str(exc_info.value)
    assert "example" in message
    assert "2026-01-15-example.md" in message
    assert "2026-01-20-example.md" in message


@pytest.mark.parametrize(
    "public_line",
    ['public: "true"', "public: yes", "public: True", "public:", "  public: true"],
)
def test_invalid_public_gate_raises(tmp_path: Path, public_line: str):
    frontmatter = (
        f"title: Example\ndate: 2026-01-15\nsummary: One sentence.\n{public_line}\n"
    )
    path = write_post(tmp_path, "2026-01-15-example.md", frontmatter)

    with pytest.raises(ValueError, match="public must be exactly"):
        build_manifest(tmp_path, [path])


def test_garbage_frontmatter_without_public_key_is_skipped(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-draft.md",
        "this is not valid frontmatter\n",
    )

    assert build_manifest(tmp_path, [path]) == []


def test_file_without_frontmatter_is_skipped(tmp_path: Path):
    posts = tmp_path / "docs" / "posts"
    posts.mkdir(parents=True)
    post = posts / "2026-01-15-draft.md"
    post.write_text("# Draft\n\nNo frontmatter here.\n", encoding="utf-8")

    assert build_manifest(tmp_path, ["docs/posts/2026-01-15-draft.md"]) == []


def test_frontmatter_date_must_match_filename_date(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(date="2026-01-20"),
    )

    with pytest.raises(ValueError, match="does not match filename date"):
        build_manifest(tmp_path, [path])


def test_crlf_input_is_parsed(tmp_path: Path):
    posts = tmp_path / "docs" / "posts"
    posts.mkdir(parents=True)
    post = posts / "2026-01-15-example.md"
    post.write_bytes(
        b"---\r\ntitle: Example\r\ndate: 2026-01-15\r\n"
        b"summary: One sentence.\r\npublic: true\r\n---\r\nBody.\r\n"
    )

    entries = build_manifest(tmp_path, ["docs/posts/2026-01-15-example.md"])

    assert entries[0]["slug"] == "example"
    assert entries[0]["content"] == "Body.\n"


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


def test_tags_are_normalized_and_deduplicated(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(tags="Inference, freetoken, INFERENCE, moe"),
    )

    entries = build_manifest(tmp_path, [path])

    assert entries[0]["tags"] == ["inference", "freetoken", "moe"]


def test_missing_tags_gives_an_empty_list(tmp_path: Path):
    path = write_post(tmp_path, "2026-01-15-example.md", public_frontmatter())

    assert build_manifest(tmp_path, [path])[0]["tags"] == []


@pytest.mark.parametrize(
    ("tags", "message"),
    [
        ("good, bad tag", "invalid tag"),
        ("one,two,three,four,five,six,seven", "between 1 and 6"),
        ("one,,two", "empty item"),
        ("one,two,", "empty item"),
    ],
)
def test_invalid_tags_raise(tmp_path: Path, tags: str, message: str):
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(tags=tags),
    )

    with pytest.raises(ValueError, match=message) as exc_info:
        build_manifest(tmp_path, [path])

    assert str(exc_info.value).startswith(path)


def test_figure_is_inlined_from_its_exact_href(tmp_path: Path):
    figure_path = write_figure(
        tmp_path,
        "example.svg",
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">\n'
        '  <path d="M0 0L10 10" fill="none" stroke="currentColor"/>\n'
        "</svg>\n",
    )
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(),
        "![Exploded view](figures/example.svg)\n",
    )

    entry = build_manifest(tmp_path, [path], {figure_path})[0]

    assert entry["figures"] == {
        "figures/example.svg": (tmp_path / figure_path).read_text(encoding="utf-8")
    }


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ('<svg viewBox="0 0 10 10"><script/></svg>', "forbidden element"),
        ('<svg viewBox="0 0 10 10" onclick="go()"/>', "forbidden attribute"),
        ('<svg viewBox="0 0 10 10"><path href="other"/></svg>', "forbidden attribute"),
    ],
)
def test_unsafe_figure_markup_is_rejected(tmp_path: Path, source: str, message: str):
    figure_path = write_figure(tmp_path, "unsafe.svg", source)
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(),
        "![Unsafe](figures/unsafe.svg)\n",
    )

    with pytest.raises(ValueError, match=message) as exc_info:
        build_manifest(tmp_path, [path], {figure_path})

    assert path in str(exc_info.value)
    assert figure_path in str(exc_info.value)


def test_missing_figure_is_rejected(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(),
        "![Missing](figures/missing.svg)\n",
    )

    with pytest.raises(ValueError, match="must exist and be tracked"):
        build_manifest(tmp_path, [path])


def test_figure_parent_escape_is_rejected(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(),
        "![Escape](figures/../secret.svg)\n",
    )

    with pytest.raises(ValueError, match="may not escape"):
        build_manifest(tmp_path, [path])


def test_non_figure_images_are_ignored(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-example.md",
        public_frontmatter(),
        "![Photo](images/photo.png)\n![Other SVG](images/diagram.svg)\n",
    )

    assert "figures" not in build_manifest(tmp_path, [path])[0]


def test_public_post_with_internal_marker_fails(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-leak.md",
        public_frontmatter(),
        "Call http://monolith-web.monolith.svc.cluster.local/api\n",
    )

    with pytest.raises(SystemExit, match=r"docs/posts/2026-01-15-leak.md:1"):
        build_manifest(tmp_path, [path])


def test_draft_post_with_internal_marker_is_skipped(tmp_path: Path):
    path = write_post(
        tmp_path,
        "2026-01-15-draft.md",
        "title: Draft\ndate: 2026-01-15\nsummary: One sentence.\npublic: false\n",
        "Call http://monolith-web.monolith.svc.cluster.local/api\n",
    )

    assert build_manifest(tmp_path, [path]) == []
