from bench.registry import load_registry, active_models, drop_model

YAML = """
models:
  - id: anthropic/claude-sonnet-4.6
    role: anchor
  - id: cheap/fast
    status: active
  - id: old/dead
    status: retired
    retired_reason: flunked
"""


def test_active_excludes_retired(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(YAML)
    reg = load_registry(p)
    ids = [m.id for m in active_models(reg)]
    assert "cheap/fast" in ids and "old/dead" not in ids


def test_drop_sets_retired_with_reason(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(YAML)
    drop_model(p, "cheap/fast", reason="too weak", date="2026-06-30")
    reg = load_registry(p)
    m = next(m for m in reg if m.id == "cheap/fast")
    assert m.status == "retired" and m.retired_reason == "too weak"
