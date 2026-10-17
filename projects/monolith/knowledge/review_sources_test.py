"""Only bounded, complete primary sources enter a review's evidence bundle."""

import base64

import pytest

from knowledge.review_sources import EvidenceUnavailable, collect_sources

REPO = "jomcgi-org/homelab"
SHA = "a" * 40


def test_pr_evidence_covers_state_head_statuses_and_checks_without_report_body():
    calls = []

    def fetch(path):
        calls.append(path)
        if "/issues/" in path:
            return {
                "number": 1,
                "state": "open",
                "pull_request": {},
                "body": "untrusted claim",
                "labels": [],
            }
        if "/pulls/" in path:
            return {
                "number": 1,
                "state": "open",
                "head": {"sha": SHA},
                "draft": True,
                "merged": False,
            }
        if "/status?" in path:
            return {
                "sha": SHA,
                "total_count": 1,
                "state": "success",
                "statuses": [{"context": "pr-checks", "state": "success"}],
            }
        return {"total_count": 0, "check_runs": []}

    sources, observed = collect_sources(
        {"title": "PR #1 is reported open", "content": None}, fetch=fetch
    )
    assert len(calls) == len(sources) == 4 and observed is not None
    assert "untrusted claim" not in str(sources)
    assert "pr-checks" in sources[2]["text"]
    assert {s["subject"] for s in sources} == {"github:1"}


def test_files_are_pinned_to_one_main_commit():
    calls = []

    def fetch(path):
        calls.append(path)
        if "/git/ref/" in path:
            return {"object": {"sha": SHA}}
        assert path.endswith("?ref=" + SHA)
        return {
            "type": "file",
            "path": path.split("/contents/", 1)[1].split("?", 1)[0],
            "encoding": "base64",
            "size": 7,
            "content": base64.b64encode(b"content").decode(),
        }

    sources, _ = collect_sources(
        {
            "title": "The contract lives in projects/app/main.py:4 and docs/guide.md",
            "content": None,
        },
        fetch=fetch,
    )
    assert len(calls) == 3 and len(sources) == 2
    assert all("/blob/" + SHA + "/" in item["url"] for item in sources)


@pytest.mark.parametrize(
    "claim",
    [
        "https://github.com/other/repo/pull/1 is open",
        "PR #1 is open and other/repo#2 is closed",
        "PR #1 is open and https://github.com/other/repo/blob/main/projects/app.py is current",
        "PR #1 and PR #2 and PR #3 and PR #4",
        "projects/../private/key.py",
    ],
)
def test_foreign_refs_excess_refs_and_traversal_never_fetch(claim):
    with pytest.raises(EvidenceUnavailable):
        collect_sources(
            {"title": claim, "content": None}, fetch=lambda _: pytest.fail("fetched")
        )


def test_missing_sources_are_explicit_and_do_not_invent_evidence():
    assert collect_sources(
        {"title": "The system is wonderful", "content": None},
        fetch=lambda _: pytest.fail("fetched"),
    ) == ([], None)


@pytest.mark.parametrize(
    "broken", ["identity", "head", "state", "pagination", "statuses", "check_sha"]
)
def test_incomplete_or_malformed_source_never_reaches_reviewer(broken):
    def fetch(path):
        if "/issues/" in path:
            return {
                "number": 2 if broken == "identity" else 1,
                "state": "open",
                "pull_request": {},
            }
        if "/pulls/" in path:
            return {
                "number": 1,
                "state": "unknown" if broken == "state" else "open",
                "draft": False,
                "merged": False,
                "head": {"sha": "bad" if broken == "head" else SHA},
            }
        if "/status?" in path:
            return {
                "sha": SHA,
                "total_count": 2 if broken == "statuses" else 0,
                "statuses": [],
            }
        if broken == "check_sha":
            return {"total_count": 1, "check_runs": [{"head_sha": "b" * 40}]}
        return {"total_count": 101 if broken == "pagination" else 0, "check_runs": []}

    with pytest.raises(EvidenceUnavailable):
        collect_sources({"title": "PR #1 is open", "content": None}, fetch=fetch)


def test_oversized_evidence_fails_instead_of_silently_truncating():
    def fetch(path):
        return {"number": 1, "state": "open", "title": "x" * 50_000}

    with pytest.raises(EvidenceUnavailable, match="byte bound"):
        collect_sources({"title": "Issue #1 is open", "content": None}, fetch=fetch)
