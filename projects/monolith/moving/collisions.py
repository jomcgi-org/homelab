"""Pure schedule collision detection for the moving planner."""

from dataclasses import dataclass
from datetime import date

from moving.models import Span, Task


@dataclass
class Collision:
    """Structured record naming what collided and the overlapping dates."""

    type: str  # "span_span" or "task_span"
    item1_id: str
    item2_id: str
    overlaps_from: date
    overlaps_to: date


def find_collisions(spans: list[Span], tasks: list[Task]) -> list[Collision]:
    """Find cross-kind span overlaps and tasks due inside any span."""
    collisions: list[Collision] = []

    # Leave exists to cover visits and trips, so overlapping them is the point,
    # and a task due during leave is not a conflict.
    for index, first in enumerate(spans):
        if first.kind == "leave":
            continue
        for second in spans[index + 1 :]:
            if second.kind == "leave":
                continue
            if first.kind == second.kind:
                continue
            overlaps_from = max(first.starts_on, second.starts_on)
            overlaps_to = min(first.ends_on, second.ends_on)
            if overlaps_from <= overlaps_to:
                collisions.append(
                    Collision(
                        type="span_span",
                        item1_id=first.id,
                        item2_id=second.id,
                        overlaps_from=overlaps_from,
                        overlaps_to=overlaps_to,
                    )
                )

    for task in tasks:
        if task.due_on is None:
            continue
        for span in spans:
            if span.kind == "leave":
                continue
            if span.starts_on <= task.due_on <= span.ends_on:
                collisions.append(
                    Collision(
                        type="task_span",
                        item1_id=task.id,
                        item2_id=span.id,
                        overlaps_from=task.due_on,
                        overlaps_to=task.due_on,
                    )
                )

    return collisions
