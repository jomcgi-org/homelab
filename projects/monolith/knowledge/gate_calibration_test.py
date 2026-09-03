"""Calibrate the extraction event and bare-value gates.

Gates are precision-first because a falsely rejected assertion is silently lost,
while a missed low-value atom is caught by the behavioural lens and the grading
loop.
"""

from __future__ import annotations

import csv
from pathlib import Path

from knowledge.extraction import _Assertion, _event_rejection, _value_rejection


TESTDATA = Path(__file__).parent / "testdata"


def _load_corpus() -> list[tuple[str, str, _Assertion]]:
    with (TESTDATA / "atoms-all.tsv").open(newline="", encoding="utf-8") as stream:
        bodies = {row[0]: row[6].strip() for row in csv.reader(stream, delimiter="\t")}
    with (TESTDATA / "atoms-graded.tsv").open(newline="", encoding="utf-8") as stream:
        grades = list(csv.reader(stream, delimiter="\t"))
    return [
        (
            note_id,
            label,
            _Assertion(
                title=note_id,
                body=bodies[note_id],
                scope="repo:jomcgi-org/homelab",
                verification_state="unverified",
                confidence=0.8,
            ),
        )
        for note_id, label, _reason in grades
    ]


def test_gates_meet_graded_corpus_thresholds():
    corpus = _load_corpus()
    value_rows = [
        (note_id, item) for note_id, label, item in corpus if label == "drop-value"
    ]
    event_rows = [
        (note_id, item) for note_id, label, item in corpus if label == "drop-event"
    ]
    keep_rows = [
        (note_id, item)
        for note_id, label, item in corpus
        if label == "keep-behavioural"
    ]
    borderline_rows = [
        (note_id, item) for note_id, label, item in corpus if label == "keep-borderline"
    ]

    value_misses = [
        note_id for note_id, item in value_rows if _value_rejection(item) is None
    ]
    event_misses = [
        note_id for note_id, item in event_rows if _event_rejection(item) is None
    ]
    keep_rejections = [
        note_id
        for note_id, item in keep_rows
        if _event_rejection(item) is not None or _value_rejection(item) is not None
    ]
    borderline_rejections = [
        note_id
        for note_id, item in borderline_rows
        if _event_rejection(item) is not None or _value_rejection(item) is not None
    ]

    assert len(value_rows) - len(value_misses) >= len(value_rows) * 0.25, (
        f"value gate misses: {value_misses}"
    )
    assert len(event_rows) - len(event_misses) >= len(event_rows) * 0.30, (
        f"event gate misses: {event_misses}"
    )
    assert keep_rejections == [], f"behavioral false rejections: {keep_rejections}"
    assert len(borderline_rejections) <= 2, (
        f"borderline false rejections: {borderline_rejections}"
    )


def test_named_behavioral_atoms_survive_both_gates():
    corpus = {note_id: item for note_id, _label, item in _load_corpus()}
    for note_id in (
        "session-sends-permit-only-same-family-model-overrides",
        "kargo-promotion-state-is-authoritative-over-git-chart-version-for-live-deployment",
    ):
        assert _event_rejection(corpus[note_id]) is None
        assert _value_rejection(corpus[note_id]) is None
