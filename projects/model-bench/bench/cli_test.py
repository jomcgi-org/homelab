import argparse

import pytest  # noqa: F401

from bench.cache import HARNESS_VERSION
from bench.cli import _prune_stale, build_parser, load_tasks


def test_parser_has_subcommands():
    p = build_parser()
    choices = p._subparsers._group_actions[0].choices
    for sub in ("run", "report", "drop", "prune", "prune-stale", "list", "snapshot"):
        assert sub in choices


def test_prune_stale_removes_only_other_versions(tmp_path, capsys):
    root = tmp_path / "results" / "m" / "t1"
    root.mkdir(parents=True)
    cur = (
        '{"task_id":"t1","task_version":"v1","model_id":"m","content_hash":"aaa",'
        '"outcome":"pass@1","attempts":[],"cost_usd":0.0,'
        f'"harness_version":"{HARNESS_VERSION}","prompt_template_hash":"x"}}'
    )
    old = cur.replace(
        f'"harness_version":"{HARNESS_VERSION}"', '"harness_version":"0.0.0"'
    )
    (root / "cur.json").write_text(cur)
    (root / "old.json").write_text(old)
    _prune_stale(argparse.Namespace(results=str(tmp_path / "results")))
    assert (root / "cur.json").exists()
    assert not (root / "old.json").exists()


def test_load_tasks_reads_pack(tmp_path):
    d = tmp_path / "tasks" / "t1"
    d.mkdir(parents=True)
    (d / "task.yaml").write_text(
        "id: t1\nversion: v1\nclass: config-plumbing\nprompt: p\n"
        'target_files: [values.yaml]\nverifier: {kind: command, args: {cmd: ["true"]}}\n'
    )
    tasks = load_tasks(tmp_path / "tasks")
    assert tasks[0].id == "t1" and tasks[0].task_class == "config-plumbing"
