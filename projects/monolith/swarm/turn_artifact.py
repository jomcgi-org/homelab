"""A typed channel from an agent turn back to the server.

Everything an agent has ever told this server arrives as prose. Review verdicts
are a regex over the last few non-empty lines (`swarm/policy.py`), failing
closed to "unparseable" with nothing to say about WHY. A JSON file an agent
writes reaches us only because the guest shim sweeps untracked files into the
turn diff, a path built for something else entirely. Neither is a contract.

This module is the contract: a node declares the artifact it must produce as a
path plus a JSON Schema, and the turn is judged against that declaration here,
SERVER-SIDE. The guest may pre-check as a courtesy, but a guest is exactly the
component whose claims cannot be trusted, so validation that matters happens
where the agent cannot reach it.

The whole-file channel is primary for a turn dispatched with a declared
artifact, and ``evaluate_content`` imposes no freshness constraint. Diff
recovery remains a fallback for turns without a direct delivery and can only
recover added files safely.

The outcome is never a dead end. A turn that fails validation is retryable with
the reasons fed back to the agent, and when retries run out the node escalates
to the conductor, which can retry it again or reshape the plan around it. There
is deliberately no "fail" branch: see `next_action`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarm.unified_diff import parse_unified_diff

# Outcome statuses. Everything except OK carries operator-readable reasons,
# because the reasons are what get handed back to the agent on a retry.
OK = "ok"
MISSING = "missing"
NOT_FRESH = "not_fresh"
UNPARSABLE = "unparsable"
INVALID = "invalid"


@dataclass
class ArtifactOutcome:
    status: str
    value: dict | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK


def extract_artifact(
    diff_text: str | None, path: str
) -> tuple[str | None, ArtifactOutcome | None]:
    """Recover a declared artifact's content from a turn's unified diff fallback.

    Returns ``(content, None)`` when the file was recovered, and
    ``(None, outcome)`` when it was not.

    Only a file the diff reports as ADDED can be recovered, and that is a real
    constraint rather than an implementation shortcut: a diff of a modified
    file carries hunks, not content, so reconstructing from one would yield
    whatever fragment happened to change and validate it as though it were the
    whole document. A partial parse that validates is worse than no parse, so a
    non-added artifact is reported as NOT_FRESH and the retry tells the agent
    to write the file from scratch.
    """
    if not diff_text:
        return None, ArtifactOutcome(MISSING, errors=[f"no diff recorded for {path}"])
    for entry in parse_unified_diff(diff_text):
        if entry.get("path") != path:
            continue
        if entry.get("status") != "added":
            return None, ArtifactOutcome(
                NOT_FRESH,
                errors=[
                    f"{path} was {entry.get('status')}, so the diff carries "
                    "hunks rather than the whole file"
                ],
            )
        patch = entry.get("patch") or ""
        lines = []
        for line in patch.splitlines():
            if line.startswith("+"):
                lines.append(line[1:])
            # "\\ No newline at end of file" and the @@ hunk headers are not
            # content; every other line in an added file's patch is a "+".
        return "\n".join(lines), None
    return None, ArtifactOutcome(MISSING, errors=[f"{path} was not written this turn"])


def evaluate(diff_text: str | None, path: str, schema: dict) -> ArtifactOutcome:
    """Extract, parse and validate one declared artifact from a turn."""
    raw, failure = extract_artifact(diff_text, path)
    if failure is not None:
        return failure
    return _evaluate_raw(raw, path, schema)


def evaluate_content(raw, path: str, schema: dict) -> ArtifactOutcome:
    """Validate a directly delivered artifact (the beside-the-diff channel).

    raw is the file's bytes or text as the guest delivered them, or None when
    the guest reported the file absent. Unlike the diff path there is no
    NOT_FRESH here: a delivered artifact is the whole file regardless of
    whether the turn created or modified it.
    """
    if raw is None:
        return ArtifactOutcome(MISSING, errors=[f"{path} was not delivered this turn"])
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return _evaluate_raw(raw, path, schema)


def _evaluate_raw(raw, path: str, schema: dict) -> ArtifactOutcome:
    """Parse and validate artifact content shared by both delivery paths."""
    import json

    try:
        value = json.loads(raw)
    except ValueError as exc:
        return ArtifactOutcome(UNPARSABLE, errors=[f"{path} is not valid JSON: {exc}"])
    errors = schema_errors(value, schema)
    if errors:
        return ArtifactOutcome(INVALID, value=value, errors=errors)
    return ArtifactOutcome(OK, value=value)


def schema_errors(value, schema: dict) -> list[str]:
    """Every schema violation, not just the first.

    One error per retry would take as many turns as the document has mistakes,
    and each turn is a guest boot. Sorted so the same document always produces
    the same message, because an unstable error list makes a retry look like
    progress when nothing changed.
    """
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    return sorted(
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator.iter_errors(value)
    )


def retry_instruction(outcome: ArtifactOutcome, path: str) -> str:
    """What the agent is told when its artifact did not pass.

    Names the file and every reason. An agent that is only told "invalid"
    rewrites at random, which burns a guest boot per guess.
    """
    reasons = "\n".join(f"- {reason}" for reason in outcome.errors)
    return (
        f"Your previous turn did not produce a usable {path}.\n{reasons}\n"
        f"Write {path} again from scratch as a single JSON document that "
        "satisfies the schema you were given. Change nothing else."
    )


# The ladder. There is no terminal failure here on purpose: a node whose
# artifact never validates escalates to the conductor, which owns the graph and
# can retry the node again or reshape the plan around it. A validation failure
# must never be able to strand a run, which is why "fail" is not a value this
# function can return.
ACCEPT = "accept"
RETRY = "retry"
ESCALATE = "escalate"


def next_action(outcome: ArtifactOutcome, attempts: int, max_attempts: int) -> str:
    """Decide what happens after one artifact evaluation.

    ``attempts`` counts evaluations already spent, including this one.
    """
    if outcome.ok:
        return ACCEPT
    if attempts < max_attempts:
        return RETRY
    return ESCALATE
