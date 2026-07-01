from bench.cli import build_parser, load_tasks


def test_parser_has_subcommands():
    p = build_parser()
    choices = p._subparsers._group_actions[0].choices
    for sub in ("run", "report", "drop", "prune", "list"):
        assert sub in choices


def test_load_tasks_reads_pack(tmp_path):
    d = tmp_path / "tasks" / "t1"
    d.mkdir(parents=True)
    (d / "task.yaml").write_text(
        "id: t1\nversion: v1\nclass: config-plumbing\nprompt: p\n"
        'target_files: [values.yaml]\nverifier: {kind: command, args: {cmd: ["true"]}}\n'
    )
    tasks = load_tasks(tmp_path / "tasks")
    assert tasks[0].id == "t1" and tasks[0].task_class == "config-plumbing"
