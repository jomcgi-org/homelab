import pytest  # noqa: F401

from bench.verifiers import get_verifier

RBAC_YAML = """
rules:
- apiGroups: ["argoproj.io"]
  resources: ["applications"]
  verbs: ["get", "list"]
"""


def test_rbac_cover_passes_when_all_calls_covered(tmp_path):
    (tmp_path / "role.yaml").write_text(RBAC_YAML)
    v = get_verifier("rbac-cover")
    r = v(
        tmp_path,
        {
            "clusterrole": "role.yaml",
            "required": [
                {"group": "argoproj.io", "resource": "applications", "verb": "list"}
            ],
        },
    )
    assert r.passed


def test_rbac_cover_fails_and_names_missing_verb(tmp_path):
    (tmp_path / "role.yaml").write_text(RBAC_YAML)
    v = get_verifier("rbac-cover")
    r = v(
        tmp_path,
        {
            "clusterrole": "role.yaml",
            "required": [
                {"group": "argoproj.io", "resource": "applications", "verb": "watch"}
            ],
        },
    )
    assert not r.passed and "watch" in r.feedback


def test_jsonmatch_compares_structured_answer(tmp_path):
    (tmp_path / "answer.json").write_text('{"verbs": ["get","list"]}')
    v = get_verifier("json-match")
    r = v(tmp_path, {"file": "answer.json", "expect": {"verbs": ["get", "list"]}})
    assert r.passed
