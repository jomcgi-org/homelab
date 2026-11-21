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
    envelope = {"status": "complete", "result": []}
    cache.store(key, envelope)
    assert cache.read(key) == envelope
    inputs[field] = "changed"
    assert cache.read(cache_key(**inputs)) is None


def test_reject_repository_tmpdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    with pytest.raises(ValueError, match="inside checkout"):
        Cache(tmp_path)
