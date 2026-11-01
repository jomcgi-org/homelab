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


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("checked", [0, 1, 2])
@pytest.mark.parametrize("tick", ["x", "X"])
def test_ticked_items_preserved_through_cleanup_and_identical_retry(
    bounded, checked, tick
):
    contract = text() + "\n- [ ] Second\n- [ ] Third"
    lines = contract.splitlines()
    lines[checked + 1] = lines[checked + 1].replace("[ ]", f"[{tick}]")
    ticked = "\n".join(lines)
    latest = (
        render("task", "b" * 20, ticked)
        if bounded
        else f"<!-- factory-gate:task:{'b' * 20} -->\n{ticked}"
    )
    before = "intro\n\n" + legacy() + "\n\n" + latest + "\n\nhuman tail"
    after, count = rewrite(before)
    assert count == 2
    assert after == before.replace(legacy(), "", 1).replace(
        latest, render("task", "b" * 20, ticked), 1
    )
    assert blocks(after)[0].text == ticked
    assert rewrite(after, render("task", "b" * 20, contract))[0] == after
    assert cleanup([issue(1, after)], None, lambda _: None, audit=True) == 0
    replacement = render("task", "c" * 20, text("changed"))
    assert blocks(rewrite(after, replacement)[0])[0].text == text("changed")


def test_marker_free_unclosed_fence_appends_without_parsing_human_body():
    body = "Human instructions\n```\nunfinished example"
    replacement = render("task", "a" * 20, text())
    assert rewrite(body, replacement) == (body + "\n\n" + replacement, 0)
    assert rewrite(body) == (body, 0)


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
        "```\n" + legacy(),
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
    assert cleanup([issue(1, updated)], None, lambda _row: None, audit=True) == 0


def issue(number, body=None, **extra):
    return {
        "number": number,
        "state": "open",
        "body": body or legacy() + "\n\n" + legacy("latest"),
        "updated_at": "2026-10-30T00:00:00Z",
        "labels": [],
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


@pytest.mark.parametrize("label", ["human", "needs-human"])
@pytest.mark.parametrize("apply", [False, True])
def test_cleanup_skips_human_holds_without_reading_or_writing(label, apply):
    snapshot = issue(1, labels=[{"name": label}])
    rows = []

    def api(*_args):
        pytest.fail("held issue must not reach apply")

    assert cleanup([snapshot], api, rows.append, apply=apply) == 0
    assert rows[0]["status"] == "skipped"
    assert rows[0]["labels"] == [label]
    assert rows[-1]["held"] == 1
    assert rows[-1]["changed"] == 0


@pytest.mark.parametrize("label", ["human", "needs-human"])
def test_apply_rechecks_hold_even_when_body_and_timestamp_are_unchanged(label):
    snapshot = issue(1)
    rows = []

    def api(path, payload=None):
        assert path == "issues/1"
        assert payload is None
        return {**snapshot, "labels": [{"name": label}]}

    assert cleanup([snapshot], api, rows.append, apply=True) == 1
    assert rows[0]["status"] == "skipped"
    assert rows[0]["labels"] == [label]
    assert rows[-1]["changed"] == 0


def test_audit_still_counts_duplicates_on_held_issues():
    rows = []
    assert (
        cleanup([issue(1, labels=["needs-human"])], None, rows.append, audit=True) == 1
    )
    assert rows[0]["status"] == "held"
    assert rows[1]["status"] == "duplicate"
    assert rows[-1]["held"] == 1
    assert rows[-1]["duplicates"] == 1
    assert rows[-1]["changed"] == 0


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
    assert rows[-1]["errors"] == 0
    assert rows[-1]["warnings"] == 1


def test_audit_single_unowned_rescope_warns_without_failing_or_writing():
    rows = []
    assert (
        cleanup(
            [issue(6991, "Conductor rescope: manual")], None, rows.append, audit=True
        )
        == 0
    )
    assert rows[0]["status"] == "warning"
    assert rows[-1]["duplicates"] == 0
    assert rows[-1]["errors"] == 0


@pytest.mark.parametrize("other", [legacy(), "Conductor rescope: another manual scope"])
def test_multiple_owned_or_unowned_rescopes_require_inspection(other):
    rows = []
    assert (
        cleanup(
            [issue(1, other + "\n\nConductor rescope: manual")],
            None,
            rows.append,
            audit=True,
        )
        == 1
    )
    assert rows[-1]["duplicates"] == 1
    assert rows[-1]["errors"] == 1


def test_strip_removes_generated_contracts_and_keeps_prose():
    from factory.orchestration.factory_gate_blocks import strip

    prose = "Human intro\n- [ ] Manual acceptance\n\nHuman end\n"
    bounded = render("task", "b" * 20, text("bounded"))
    body = (
        "Human intro\n- [ ] Manual acceptance\n\n"
        + legacy()
        + "\n\n"
        + bounded
        + "\n\nHuman end\n"
    )
    stripped = strip(body)
    assert legacy() not in stripped and bounded not in stripped
    assert " ".join(stripped.split()) == " ".join(prose.split())
    assert strip(prose) == prose
    # Ambiguous markers are left alone rather than guessed at.
    malformed = "<!-- factory-gate:task -->\nno contract here"
    assert strip(malformed) == malformed


def test_identical_gate_rendered_differently_keeps_ticks():
    # The scope line renders without the echoed suffix now, but the gate and
    # its identity are unchanged, so a retry must carry the person's ticks.
    old = render("task", "a" * 20, text("same").replace("- [ ]", "- [x]"))
    body = "intro\n\n" + old
    new_text = text("same").replace(
        "Conductor rescope: same", "Conductor rescope: same, reworded"
    )
    updated, count = rewrite(body, render("task", "a" * 20, new_text))
    assert count == 1
    assert "- [x] Verify same" in updated and "reworded" in updated


def test_identical_gate_from_a_new_task_keeps_ticks_under_the_new_task():
    old = render("old-task", "a" * 20, text("same").replace("- [ ]", "- [x]"))
    updated, _count = rewrite(
        "intro\n\n" + old, render("new-task", "a" * 20, text("same"))
    )
    block = blocks(updated)[0]
    assert block.task == "new-task" and "- [x] Verify same" in block.text
