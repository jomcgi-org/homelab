import pytest  # noqa: F401

from bench.cache import cell_key, cell_path, is_cached, HARNESS_VERSION


def test_cell_key_is_stable_and_order_independent(tmp_path):
    inputs = dict(
        prompt="p",
        fixture_hash="fh",
        verifier_repr="vr",
        model_id="m",
        params_repr="pr",
    )
    k1 = cell_key(**inputs)
    k2 = cell_key(**inputs)
    assert k1 == k2 and len(k1) == 12


def test_cell_key_changes_when_verifier_changes():
    base = dict(
        prompt="p",
        fixture_hash="fh",
        verifier_repr="v1",
        model_id="m",
        params_repr="pr",
    )
    assert cell_key(**base) != cell_key(**{**base, "verifier_repr": "v2"})


def test_is_cached_true_only_when_file_exists(tmp_path):
    p = cell_path(tmp_path, "openai/gpt-x", "task-1", "abc123def456")
    assert not is_cached(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    assert is_cached(p)
