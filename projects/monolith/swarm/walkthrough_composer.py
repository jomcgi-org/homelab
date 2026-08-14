"""Compose the session-tier walkthrough from recorded evidence."""

from __future__ import annotations

from typing import Any


_LARGE_ACTIVITY_COUNT = 12


def _activities(value: Any) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("activities", [])
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _testimony(
    rationale: dict, turn_seq: int, points: list[dict] | None = None
) -> dict:
    return {
        "turn": rationale.get("turn", turn_seq),
        "attempt": rationale.get("attempt", 1),
        "points": points if points is not None else list(rationale.get("paths", [])),
    }


def _file_change(item: dict) -> dict:
    return {
        "additions": item.get("additions", 0),
        "deletions": item.get("deletions", 0),
        "status": item.get("status"),
    }


def _path_order(activities: list[dict]) -> list[str]:
    result = []
    for activity in activities:
        if activity.get("type") in {"edit", "write"} and isinstance(
            activity.get("file_path"), str
        ):
            if activity["file_path"] not in result:
                result.append(activity["file_path"])
    return result


def _run_paths(activity: dict) -> set[str]:
    if isinstance(activity.get("file_path"), str):
        return {activity["file_path"]}
    for key in ("file_paths", "files", "output_files", "produced_files"):
        value = activity.get(key)
        if isinstance(value, list):
            return {path for path in value if isinstance(path, str)}
    return set()


def _area(rationale: dict, point: dict) -> str | None:
    if isinstance(point.get("area"), str):
        return point["area"]
    areas = rationale.get("areas")
    if isinstance(areas, dict) and isinstance(areas.get(point.get("path")), str):
        return areas[point["path"]]
    return None


def _mechanical_steps(files: list[dict], activities: list[dict]) -> list[dict]:
    mechanical = [item for item in files if item.get("classification") == "mechanical"]
    runs = [item for item in activities if item.get("type") == "run"]
    if not mechanical or not runs:
        return []
    remaining = {item.get("path"): item for item in mechanical}
    groups: list[tuple[dict, list[dict]]] = []
    for run in runs:
        explicit = _run_paths(run)
        selected = [remaining.pop(path) for path in explicit if path in remaining]
        groups.append((run, selected))
    # The compare has no per-file producer field. Unclaimed outputs belong to
    # the last run that preceded them, which is the only defensible ordering
    # available when the activity ingest omitted generator output paths.
    if remaining:
        groups[-1][1].extend(remaining.values())
    return [
        {
            "type": "mechanical",
            "register": "fact",
            "count": len(items),
            "generator_activity": run,
        }
        for run, items in groups
        if items
    ]


def _rung4(activities: list[dict]) -> dict:
    paths = _path_order(activities)
    return {
        "rung": 4,
        "ephemeral": False,
        "steps": [],
        "stats": {
            "total_files": len(paths),
            "authored_files": len(paths),
            "activities": paths,
        },
        "message": "Files touched by tools this turn; decline to offer walkthrough without intent structure",
    }


def compose_walkthrough(
    session_id: int,
    turn_seq: int,
    compare_output: dict | None,
    rationale_output: dict | None,
    usage_json_activities: dict | list | None,
) -> dict:
    """Compose one turn's session walkthrough.

    ``session_id`` is accepted as part of the public contract. It is not
    copied into steps because the console already has it in the route.
    """
    del session_id
    rationale = rationale_output if isinstance(rationale_output, dict) else {}
    activities = _activities(usage_json_activities)
    parsed = rationale.get("parse_status") == "parsed"
    compare_rung = (
        compare_output.get("resolution_rung", 1)
        if isinstance(compare_output, dict)
        else None
    )
    has_compare = compare_rung in (1, 2)

    if not has_compare and not parsed:
        authored = _path_order(activities)
        if len(authored) > _LARGE_ACTIVITY_COUNT:
            return _rung4(activities)
        return {
            "rung": 5,
            "ephemeral": False,
            "steps": [],
            "message": "No activity recorded",
        }
    if not has_compare and parsed:
        steps = [
            {
                "type": "authored",
                "register": "testimony",
                "file_path": item.get("path"),
                "testimony": _testimony(rationale, turn_seq, [item]),
            }
            for item in rationale.get("paths", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        if steps and rationale.get("deviations"):
            steps[0]["testimony"]["points"].extend(
                {"deviation": item} for item in rationale["deviations"]
            )
        elif rationale.get("deviations"):
            steps.append(
                {
                    "type": "authored",
                    "register": "testimony",
                    "testimony": _testimony(
                        rationale,
                        turn_seq,
                        [{"deviation": item} for item in rationale["deviations"]],
                    ),
                }
            )
        steps.extend(
            {
                "type": "authored",
                "register": "fact",
                "file_path": path,
                "file_change": {"additions": 0, "deletions": 0, "status": "touched"},
            }
            for path in _path_order(activities)
            if path
            not in {
                item.get("path")
                for item in rationale.get("paths", [])
                if isinstance(item, dict)
            }
        )
        return {
            "rung": 3,
            "ephemeral": False,
            "steps": steps,
            "stats": {"authored_files": len(_path_order(activities))},
            "message": "Limited walkthrough: testimony and activities only",
        }

    compare = compare_output
    files = [
        item
        for item in compare.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ]
    by_path = {item["path"]: item for item in files}
    authored_paths = {
        item["path"] for item in files if item.get("classification") == "authored"
    }
    point_by_path = {
        item.get("path"): item
        for item in rationale.get("paths", [])
        if isinstance(item, dict)
    }
    ordered = [
        item["path"]
        for item in rationale.get("paths", [])
        if isinstance(item, dict) and item.get("path") in authored_paths
    ]
    for path in _path_order(activities) + [item["path"] for item in files]:
        if path in authored_paths and path not in ordered:
            ordered.append(path)
    steps = []
    for path in ordered:
        item = by_path[path]
        step = {
            "type": "authored",
            "register": "fact",
            "file_path": path,
            "file_change": _file_change(item),
        }
        point = point_by_path.get(path)
        if point is not None:
            if _area(rationale, point) is not None:
                step["area"] = _area(rationale, point)
            step["testimony"] = _testimony(rationale, turn_seq, [point])
        steps.append(step)
    steps.extend(_mechanical_steps(files, activities))
    for path in sorted(set(compare.get("unexplained_files", []))):
        if path in by_path:
            steps.append(
                {
                    "type": "unexplained",
                    "register": "fact",
                    "file_path": path,
                    "file_change": _file_change(by_path[path]),
                    "label": "Unexplained file",
                }
            )
    for path in sorted(set(compare.get("contradicted_paths", []))):
        point = point_by_path.get(path, {"path": path})
        steps.append(
            {
                "type": "contradiction",
                "register": "testimony",
                "label": "Contradicted path",
                "testimony": _testimony(rationale, turn_seq, [point]),
            }
        )
    if compare.get("stats", {}).get("truncated_at") or compare.get("truncated"):
        steps.append(
            {
                "type": "truncation",
                "register": "fact",
                "label": "GitHub files truncated",
            }
        )
    if compare.get("activities_truncated"):
        steps.append(
            {"type": "truncation", "register": "fact", "label": "activities truncated"}
        )
    return {
        "rung": compare_rung,
        "ephemeral": compare_rung == 2,
        "steps": steps,
        "stats": compare.get("stats", {}),
        **(
            {
                "message": "This walkthrough becomes unavailable once this branch is deleted"
            }
            if compare_rung == 2
            else {}
        ),
    }
