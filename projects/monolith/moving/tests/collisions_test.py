"""Boundary-focused tests for pure moving collision detection."""

from datetime import date

from moving.collisions import Collision, find_collisions
from moving.models import Span, Task


def _span(
    span_id: str,
    kind: str,
    starts_on: date,
    ends_on: date,
) -> Span:
    return Span(
        id=span_id,
        kind=kind,
        label=span_id,
        starts_on=starts_on,
        ends_on=ends_on,
    )


def test_leave_spans_never_collide():
    spans = [
        _span("leave", "leave", date(2026, 9, 1), date(2026, 9, 10)),
        _span("visitor", "visitor", date(2026, 9, 2), date(2026, 9, 8)),
        _span("trip", "trip", date(2026, 9, 3), date(2026, 9, 6)),
    ]
    task = Task(id="task", title="Pack", due_on=date(2026, 9, 1))

    collisions = find_collisions(spans, [task])

    pairs = {(collision.item1_id, collision.item2_id) for collision in collisions}
    assert ("leave", "visitor") not in pairs
    assert ("leave", "trip") not in pairs
    assert ("task", "leave") not in pairs
    assert ("visitor", "trip") in pairs


def test_task_due_only_inside_leave_span_is_not_a_collision():
    spans = [_span("leave", "leave", date(2026, 9, 1), date(2026, 9, 10))]
    task = Task(id="task", title="Pack", due_on=date(2026, 9, 5))
    assert find_collisions(spans, [task]) == []


def test_overlapping_different_kind_spans_collide():
    spans = [
        _span("visitor", "visitor", date(2026, 9, 1), date(2026, 9, 5)),
        _span("work", "work", date(2026, 9, 3), date(2026, 9, 8)),
    ]
    assert find_collisions(spans, []) == [
        Collision(
            type="span_span",
            item1_id="visitor",
            item2_id="work",
            overlaps_from=date(2026, 9, 3),
            overlaps_to=date(2026, 9, 5),
        )
    ]


def test_same_kind_spans_do_not_collide():
    spans = [
        _span("first", "work", date(2026, 9, 1), date(2026, 9, 5)),
        _span("second", "work", date(2026, 9, 2), date(2026, 9, 6)),
    ]
    assert find_collisions(spans, []) == []


def test_span_boundaries_are_inclusive():
    spans = [
        _span("trip", "trip", date(2026, 9, 1), date(2026, 9, 4)),
        _span("move", "move", date(2026, 9, 4), date(2026, 9, 8)),
    ]
    collision = find_collisions(spans, [])[0]
    assert collision.overlaps_from == date(2026, 9, 4)
    assert collision.overlaps_to == date(2026, 9, 4)


def test_task_due_inside_span_collides():
    span = _span("move", "move", date(2026, 9, 4), date(2026, 9, 8))
    task = Task(id="task", track="admin", title="Forms", due_on=date(2026, 9, 6))
    assert find_collisions([span], [task]) == [
        Collision(
            type="task_span",
            item1_id="task",
            item2_id="move",
            overlaps_from=date(2026, 9, 6),
            overlaps_to=date(2026, 9, 6),
        )
    ]


def test_zero_length_span_collides_on_its_single_day():
    day = date(2026, 9, 4)
    span = _span("move", "move", day, day)
    task = Task(id="task", track="admin", title="Forms", due_on=day)
    assert find_collisions([span], [task]) == [
        Collision(
            type="task_span",
            item1_id="task",
            item2_id="move",
            overlaps_from=day,
            overlaps_to=day,
        )
    ]


def test_task_span_boundaries_are_inclusive_and_missing_due_date_is_skipped():
    span = _span("visitor", "visitor", date(2026, 9, 4), date(2026, 9, 8))
    tasks = [
        Task(id="start", track="people", title="Start", due_on=date(2026, 9, 4)),
        Task(id="end", track="people", title="End", due_on=date(2026, 9, 8)),
        Task(id="none", track="people", title="No due date"),
    ]
    collisions = find_collisions([span], tasks)
    assert [
        (collision.item1_id, collision.overlaps_from) for collision in collisions
    ] == [
        ("start", date(2026, 9, 4)),
        ("end", date(2026, 9, 8)),
    ]
