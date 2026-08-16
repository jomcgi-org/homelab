import pytest  # noqa: F401

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


def test_load_registry_keeps_api_model_and_extra_body(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(
        """
models:
  - id: qwen/qwen3.8-27b
    display_name: qwen3.8-27b
    status: experimental
    api_model: qwen3.6-27b
    extra_body:
      chat_template_kwargs:
        reasoning_effort: xhigh
"""
    )
    m = load_registry(p)[0]
    assert m.api_model == "qwen3.6-27b"
    assert m.status == "experimental"
    assert m.extra_body == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}
    assert m.id not in [x.id for x in active_models(load_registry(p))]


def test_drop_sets_retired_with_reason(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(YAML)
    drop_model(p, "cheap/fast", reason="too weak", date="2026-06-30")
    reg = load_registry(p)
    m = next(m for m in reg if m.id == "cheap/fast")
    assert m.status == "retired" and m.retired_reason == "too weak"
