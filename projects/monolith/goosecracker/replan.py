"""Parse goose's structured ``replan`` escape-hatch signal (Task 7).

The runtime router (``goosecracker.router_render``) gives goose an OPTIONAL
``replan`` object in its ``recipe__final_output`` response schema. When goose
finds the pre-decided plan does not fit the task it actually opened, it emits a
populated ``replan`` (``reason`` / ``what_i_learned`` / ``suggested_focus``)
instead of forcing a bad result. That structured object is returned verbatim in
``AgentResult.Result`` (goose's ``recipe__final_output`` JSON string), which the
runner reads out of the fc-invoke response body as ``data["result"]``.

``parse_replan`` extracts that signal defensively: the result may be the bare
JSON object, a fenced block, or a JSON object on the trailing line of a longer
transcript. It returns a :class:`ReplanRequest` ONLY when a ``replan`` object
with at least one non-empty field is present, and ``None`` for everything else
(absent, all-empty, malformed, or non-JSON). It never raises: a parse problem
must degrade to "no replan requested" (finalize with the current result), never
crash the delivery path.

This module deliberately owns :class:`ReplanRequest` and depends on nothing in
``chat`` (only the stdlib), so the host-side replan loop in ``runner`` can pass
its plain-string fields to ``chat.orchestrator.replan`` without ``orchestrator``
importing back into ``goosecracker`` (avoiding a layering cycle).
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplanRequest:
    """A goose-emitted request for a capped re-plan.

    All three fields are plain strings sourced from the router's ``replan``
    object; the runner passes them straight to ``chat.orchestrator.replan``.
    """

    reason: str
    what_i_learned: str
    suggested_focus: str


def _strip_fences(text: str) -> str:
    """Strip surrounding whitespace and an optional ```json ... ``` fence.

    Mirrors ``chat.orchestrator._strip_fences`` (inlined here to keep this
    module free of any ``chat`` import, avoiding an import cycle): the model
    occasionally wraps its final JSON in a fenced block, so unwrap it before
    parsing. Anything else is left untouched for ``json.loads`` to accept or
    reject.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _locate_json_object(result_text: str) -> dict | None:
    """Locate the final_output JSON object in a (possibly wrapped) result string.

    Tries, in order: the whole (fence-stripped) string as one JSON object, then
    the last brace-delimited non-empty line of the transcript (the shape goose's
    ``response.json_schema`` emits, mirroring
    ``runner._parse_structured_result``). Returns the dict or ``None``; never
    raises.
    """
    stripped = _strip_fences(result_text)
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        return obj

    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None  # the last non-empty line is not a JSON object
    return None


def parse_replan(result_text: str) -> ReplanRequest | None:
    """Return a :class:`ReplanRequest` iff a non-empty ``replan`` is present.

    Returns ``None`` when the result is empty, carries no ``replan`` object, or
    carries an all-empty ``replan`` (goose left the escape hatch untouched), and
    when the result cannot be parsed at all. Never raises.
    """
    if not result_text:
        return None
    obj = _locate_json_object(result_text)
    if not isinstance(obj, dict):
        return None
    replan = obj.get("replan")
    if not isinstance(replan, dict):
        return None
    reason = str(replan.get("reason") or "").strip()
    what_i_learned = str(replan.get("what_i_learned") or "").strip()
    suggested_focus = str(replan.get("suggested_focus") or "").strip()
    if not (reason or what_i_learned or suggested_focus):
        return None
    return ReplanRequest(
        reason=reason,
        what_i_learned=what_i_learned,
        suggested_focus=suggested_focus,
    )
