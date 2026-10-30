"""Offline fixtures for migration and the manual open-issue cleanup."""

import pytest

from factory.orchestration.factory_gate_blocks import blocks, render, rewrite
from factory.orchestration.factory_gate_cleanup import cleanup, open_issues


def text(scope="new"):
    return (
        f"Conductor rescope: {scope}. Deliver repository changes default-off or staged. "
        "Operational acceptance remains on the issue; do not close it.\n- [ ] Verify "
        + scope
    )


def legacy(task="old", identity="a" * 20):
    return f"<!-- factory-gate:{task}:{identity} -->\n{text(task)}"


@pytest.mark.parametrize("tasks", [("one", "one"), ("historical", "current")])
def test_legacy_collapse_keeps_newest_and_all_surrounding_bytes(tasks):
    first, last = map(legacy, tasks)
    before = (
        "Human intro\n- [ ] Manual acceptance\n\n"
        + first
        + "\n\nHuman middle\n\n"
        + last
        + "\n\n- [ ] Manual tail\nHuman end\n"
    )
    updated, count = rewrite(before)
    assert count == 2
    assert updated == before.replace(first, "", 1).replace(
        last, render(tasks[1], "a" * 20, text(tasks[1])), 1
    )
    assert len(blocks(updated)) == 1
    assert cleanup([issue(1, updated)], None, lambda _row: None, audit=True) == 0
    assert rewrite(updated)[0] == updated


def test_append_replace_retry():
    first = render("task", "a" * 20, text("first"))
    second = render("task", "b" * 20, text("second"))
    once, count = rewrite("intro", first)
    assert count == 0
    assert rewrite(once, first)[0] == once
    updated, count = rewrite(once, second)
    assert count == 1
    assert updated == "intro\n\n" + second


@pytest.mark.parametrize(
    "body",
    [
        "<!-- factory-gate:task -->\n" + text(),
        "<!-- /factory-gate:task -->",
        "<!-- factory-gate:task:bad -->\n" + text(),
        render("one", "a" * 20, text()).replace(
            "/factory-gate:one", "/factory-gate:two"
        ),
        render("one", "a" * 20, text()).replace("- [ ] Verify new", "unknown content"),
        legacy() + "\n<!-- factory-gate:task -->",
        "<!-- factory-gate:task:"
        + "a" * 20
        + " -->\nConductor rescope: human\n- [ ] Manual",
        render("one", "a" * 20, text()).replace(
            "<!-- factory-gate-hash:",
            "<!-- factory-gate:other -->\n<!-- factory-gate-hash:",
        ),
    ],
)
def test_unknown_boundaries_fail_without_rewriting(body):
    with pytest.raises(ValueError):
        rewrite(body)


def test_inline_and_fenced_marker_examples_are_preserved():
    examples = (
        "Use `<!-- factory-gate:{task_id} -->` in the issue.\n\n"
        "```markdown\n" + legacy() + "\n```\n\n"
        "~~~\n<!-- factory-gate:example -->\n~~~\n\n"
    )
    original = examples + legacy("actual")
    updated, count = rewrite(original)
    assert count == 1
    assert updated.startswith(examples)
    assert len(blocks(updated)) == 1


def issue(number, body=None, **extra):
    return {
        "number": number,
        "state": "open",
        "body": body or legacy() + "\n\n" + legacy("latest"),
        "updated_at": "2026-10-30T00:00:00Z",
        **extra,
    }


def test_open_only_full_pagination_and_offline_dry_run():
    pages = [
        [issue(n) for n in range(1, 101)],
        [issue(101, pull_request={}), issue(102, state="closed"), issue(103)],
    ]
    calls = []

    def api(path):
        calls.append(path)
        return pages[len(calls) - 1]

    rows = []
    assert cleanup(open_issues(api), api, rows.append) == 0
    assert calls == [
        "issues?state=open&per_page=100&page=1",
        "issues?state=open&per_page=100&page=2",
    ]
    assert rows[-1]["changed"] == 101
    assert {r["number"] for r in rows[:-1]} == set(range(1, 101)) | {103}


def test_apply_rechecks_and_rerun_is_noop():
    current = issue(1)
    writes = []

    def api(path, payload=None):
        assert path == "issues/1"
        if payload:
            writes.append(payload)
            current.update(payload)
        return current.copy()

    rows = []
    assert cleanup([current.copy()], api, rows.append, apply=True) == 0
    assert len(writes) == 1
    assert rows[0]["status"] == "write_started"
    assert rows[1]["status"] == "changed"
    assert cleanup([current.copy()], api, rows.append, apply=True) == 0
    assert len(writes) == 1
    assert cleanup([current.copy()], api, rows.append, audit=True) == 0


@pytest.mark.parametrize(
    "delta",
    [
        {"state": "closed"},
        {"body": "edited"},
        {"updated_at": "later"},
        {"pull_request": {}},
    ],
)
def test_stale_or_closed_issue_never_written(delta):
    snapshot = issue(1)

    def api(path, payload=None):
        assert payload is None
        return {**snapshot, **delta}

    rows = []
    assert cleanup([snapshot], api, rows.append, apply=True) == 1
    assert rows[0]["status"] == "skipped"


def test_failed_page_and_uncertain_write_stop_and_report():
    def failed_page(path):
        if "page=1" in path:
            return [issue(n) for n in range(100)]
        raise RuntimeError("page unavailable")

    rows = []
    assert cleanup(open_issues(failed_page), failed_page, rows.append) == 1
    assert "coverage" in rows[-2]
    writes = []

    def failed_write(path, payload=None):
        if payload:
            writes.append(path)
            raise TimeoutError("unknown write outcome")
        return issue(1)

    assert cleanup([issue(1), issue(2)], failed_write, rows.append, apply=True) == 1
    assert writes == ["issues/1"]


def test_audit_duplicates_ambiguity_and_closed_issues():
    rows = []
    assert (
        cleanup(
            [issue(1), issue(2, state="closed"), issue(3, "Conductor rescope: manual")],
            None,
            rows.append,
            audit=True,
        )
        == 1
    )
    assert rows[-1]["duplicates"] == 1
    assert rows[-1]["errors"] == 1
