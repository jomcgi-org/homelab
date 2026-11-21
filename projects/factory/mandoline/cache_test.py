import json

import pytest

from projects.factory.mandoline.cache import Cache, cache_key


@pytest.mark.parametrize(
    "field", ["tree", "configs", "tool_digest", "pack_digests", "invocation"]
)
def test_changed_input_misses_cache(tmp_path, monkeypatch, field):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    cache = Cache(tmp_path / "checkout")
    inputs = dict(
        tree="tree-a",
        configs={"rules": "a"},
        tool_digest="tool",
        pack_digests={"default": "a"},
        invocation=["diff"],
    )
    key = cache_key(**inputs)
    envelope = dict(
        status="complete",
        result=[],
        commit="a" * 40,
        tool_version="0.25.0",
        pack_digests={},
        gaps=[],
    )
    cache.store(key, envelope)
    assert cache.read(key) == envelope
    inputs[field] = "changed"
    assert cache.read(cache_key(**inputs)) is None


def test_reject_repository_tmpdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    with pytest.raises(ValueError, match="inside checkout"):
        Cache(tmp_path)


@pytest.mark.parametrize(
    "entry",
    [
        [],
        None,
        {"status": "complete"},
        *[
            dict(
                status=status,
                result=[],
                commit="a" * 40,
                tool_version="0.25.0",
                pack_digests={},
                gaps=[],
            )
            for status in (
                "partial",
                "unsupported",
                "timeout",
                "error",
                "truncated",
                "missing_dependency",
            )
        ],
        dict(
            status="complete",
            result=[],
            commit="a" * 40,
            tool_version="0.25.0",
            pack_digests={},
            gaps=["skipped file"],
        ),
    ],
)
def test_invalid_existing_entry_is_a_miss(tmp_path, monkeypatch, entry):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    cache = Cache(tmp_path / "checkout")
    key = "b" * 64
    cache.path(key).write_text(json.dumps(entry))
    assert cache.read(key) is None
