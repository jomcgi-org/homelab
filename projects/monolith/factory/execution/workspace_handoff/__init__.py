"""Server-owned workspace restore context; never accepted from a model prompt."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class WorkspaceHandoff:
    lineage_id: str
    expected_fingerprint: str
    producer_ember_session_id: str
    handoff_sha256: str
    preflight_json: str | None = None

    def to_json(self) -> str:
        if self.preflight_json is not None:
            if (
                not isinstance(self.preflight_json, str)
                or len(self.preflight_json.encode()) > 256 * 1024
            ):
                raise ValueError("invalid workspace handoff preflight")
            payload = json.loads(self.preflight_json)
            if type(payload) is not dict:
                raise ValueError("invalid workspace handoff preflight")
            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            if canonical != self.preflight_json:
                raise ValueError("workspace handoff preflight must be canonical")
            try:
                declaration = payload["handoff"]
                producer = declaration["producer"]
                digest = hashlib.sha256(
                    json.dumps(
                        declaration,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                if (
                    payload["version"] != 1
                    or payload["expected_workspace_fingerprint"]
                    != self.expected_fingerprint
                    or producer["lineage_id"] != self.lineage_id
                    or producer["session_id"] != self.producer_ember_session_id
                    or digest != self.handoff_sha256
                ):
                    raise ValueError("workspace handoff preflight identity mismatch")
            except (KeyError, TypeError) as exc:
                raise ValueError(
                    "invalid workspace handoff preflight identity"
                ) from exc
        for value in (self.lineage_id, self.producer_ember_session_id):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise ValueError("invalid workspace handoff identity")
        for value in (self.expected_fingerprint, self.handoff_sha256):
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise ValueError("invalid workspace handoff digest")
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "WorkspaceHandoff":
        result = cls(**json.loads(value))
        result.to_json()
        return result


def first_turn_handoff(row, turn_seq: int, existing_ember):
    """Only the initial reviewer turn restores the producer's workspace.

    The transport must verify the fingerprint even on a retry with a binding.
    A new CLI identity prevents resuming the producer's transcript. Later turns
    use the reviewer's own normal continuation and recovery path.
    """
    if turn_seq != 1 or getattr(row, "workspace_handoff_json", None) is None:
        return None
    handoff = WorkspaceHandoff.from_json(row.workspace_handoff_json)
    if (
        existing_ember is not None
        and existing_ember.session_id == handoff.producer_ember_session_id
    ):
        raise ValueError("reviewer cannot reuse producer guest")
    return handoff
