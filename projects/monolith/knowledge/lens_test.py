"""Regression tests for source-specific knowledge extraction lenses."""

from knowledge.extraction import _lens, build_extraction_prompt
from knowledge.models import RawInput


class _Embedder:
    async def embed(self, _text):
        return [0.0] * 1024


def _raw(source: str) -> RawInput:
    return RawInput(
        raw_id=f"raw-{source}",
        path=f"raws/{source}.md",
        source=source,
        content_hash="hash",
        extra={"repo": "jomcgi-org/homelab"},
    )


def test_session_lens_requires_behavioural_shapes_and_no_padding():
    lens = _lens(_raw("claude-session"))

    assert "only if it states how something BEHAVES, not what a value is" in lens
    assert "causal" in lens
    assert "X does Y because Z" in lens
    assert "constraint" in lens
    assert "must be external or snapshot recovery repeats stale state" in lens
    assert "operational state with validity" in lens
    assert "`valid_from` and `observed_at`" in lens
    assert "measured" in lens
    assert "number was observed rather than read from a file" in lens
    assert "ONLY as the mechanism of a behaviour" in lens
    assert "past the daily cap the drainer defers the rest an hour" in lens
    assert "never as a bare value such as `the cap is 40`" in lens
    for skipped in (
        "restatements of constants",
        "docstrings",
        "READMEs",
        "ADRs",
        "commit messages",
        "anything a reader gets by opening one file",
    ):
        assert skipped in lens
    assert "`edges.contradicts`" in lens
    assert "tool output in the transcript shows it" in lens
    assert "the agent saying so is `unverified`" in lens
    assert (
        "An empty `assertions` list is the normal answer for a short or read-only "
        "session; do not pad"
    ) in lens


def test_repo_diff_lens_is_unchanged():
    assert _lens(_raw("repo-diff")) == (
        "This is the diff merged into main between base and head. Emit repository "
        "facts that the diff CHANGES or ADDS: configuration values, defaults, "
        "contracts, invariants, behaviours, names. For each fact that replaces an "
        "existing one among the related notes, set `edges.supersedes` to that note "
        "id. Prefer verified with file:line evidence from the checkout at head. "
        "Skip generated files, formatting, and anything already stated identically "
        "by a related note. Also list `doc_drift`: places where a document in the "
        "checkout (README.md files, docs/**, .claude/**, AGENTS.md, "
        "ARCHITECTURE.md, runbooks, ADRs marked Accepted) makes a claim the diff "
        "contradicts."
    )


def test_ember_prompt_embeds_the_session_lens(monkeypatch):
    monkeypatch.setattr(
        "knowledge.extraction.raw_store.fetch_raw", lambda _hash: "body"
    )
    monkeypatch.setattr("knowledge.extraction.EmbeddingClient", _Embedder)
    monkeypatch.setattr(
        "knowledge.store.KnowledgeStore.search_notes_with_context",
        lambda *_args, **_kwargs: [],
    )

    prompt = build_extraction_prompt(object(), _raw("ember-session"))

    assert "Source: ember-session" in prompt
    assert "only if it states how something BEHAVES, not what a value is" in prompt
    assert "An empty `assertions` list is the normal answer" in prompt
