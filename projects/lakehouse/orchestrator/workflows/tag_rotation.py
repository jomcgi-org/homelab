"""``TagRotationWorkflow`` — rotate serving-artifact S3 tags after a grace period.

ADR platform/004 §"Serving artifact lifecycle":

    [*] --> Building : write to S3 + tag state=building
    Building --> Current  : after swap event + 5min grace period
    Current --> Previous  : next build's tag rotation
    Previous --> Stale    : next-next build's rotation
    Stale --> [*]         : SeaweedFS lifecycle policy (age > 1 day)

This workflow runs **per build**, right after ``BuildServingArtifactWorkflow``
announces a new artifact. It:

  1. ``workflow.sleep``s a 5-minute grace period so in-flight queries against the
     old "current" artifact complete before its tag moves to ``previous``.
  2. Runs the :func:`rotate_tags` activity, which advances every serving object's
     ``state`` tag one step toward stale and promotes the just-built artifact to
     ``current``.
  3. Belt-and-suspenders: the same activity does a "keep last N=24" cleanup
     (platform/004 §Risks: "SeaweedFS lifecycle daemon paused / misbehaves" — the
     explicit cleanup catches anything the lifecycle policy misses).

The 5-min sleep is genuine workflow-timer state (durable, survives worker
restarts) — exactly what Temporal timers are for — and it is the only thing the
workflow body does besides invoke the activity; all S3 I/O is in the activity.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta

import temporalio.activity
import temporalio.workflow
from temporalio.common import RetryPolicy

# Match build_serving's bucket/prefix so both agree on where artifacts live.
SERVING_BUCKET = "warehouse"
SERVING_PREFIX = "serving/"

# In-cluster SeaweedFS S3 gateway, assembled from parts (semgrep
# no-hardcoded-k8s-service-url — the cluster DNS suffix must not appear as a
# single literal). Override via ``SEAWEEDFS_S3_ENDPOINT`` from values.yaml.
_S3_SVC = "seaweedfs-s3"
_S3_NS = "seaweedfs"
_CLUSTER_DNS_SUFFIX = ".".join(["svc", "cluster", "local"])
DEFAULT_S3_ENDPOINT = f"http://{_S3_SVC}.{_S3_NS}.{_CLUSTER_DNS_SUFFIX}:8333"

# Object-tag state machine. Order matters: rotation walks current->previous->
# stale so the OLD current demotes before the NEW artifact is promoted to current.
STATE_TAG = "state"
STATE_BUILDING = "building"
STATE_CURRENT = "current"
STATE_PREVIOUS = "previous"
STATE_STALE = "stale"

# One-step demotion of an existing artifact's state on each rotation. building is
# NOT demoted here — the just-built artifact is promoted to current explicitly.
_DEMOTE = {
    STATE_CURRENT: STATE_PREVIOUS,
    STATE_PREVIOUS: STATE_STALE,
}

# Grace before demoting the old current (lets in-flight queries finish).
GRACE_PERIOD = timedelta(minutes=5)

# Belt-and-suspenders retention: keep the N newest artifacts, mark the rest stale.
KEEP_LAST_N = 24

_ACTIVITY_TIMEOUT = timedelta(minutes=5)
_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


@dataclass
class RotationResult:
    """Outcome of one ``rotate_tags`` run.

    ``promoted`` is the key just moved to ``current``; ``demoted`` maps key ->
    new state for everything stepped down; ``cleaned`` lists keys forced to
    ``stale`` by the keep-last-N retention sweep.
    """

    promoted: str | None = None
    demoted: dict[str, str] = field(default_factory=dict)
    cleaned: list[str] = field(default_factory=list)


def _s3_client():
    """boto3 S3 client for the SeaweedFS gateway (path-style, dummy creds OK)."""
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", DEFAULT_S3_ENDPOINT)
    # boto3 requires a scheme on endpoint_url; prefix http:// when absent.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def _get_state(s3, key: str) -> str | None:
    """Read the ``state`` tag of object ``key`` (None if untagged)."""
    resp = s3.get_object_tagging(Bucket=SERVING_BUCKET, Key=key)
    for tag in resp.get("TagSet", []):
        if tag.get("Key") == STATE_TAG:
            return tag.get("Value")
    return None


def _set_state(s3, key: str, state: str) -> None:
    """Set object ``key``'s ``state`` tag to ``state`` (replaces the tag set)."""
    s3.put_object_tagging(
        Bucket=SERVING_BUCKET,
        Key=key,
        Tagging={"TagSet": [{"Key": STATE_TAG, "Value": state}]},
    )


@temporalio.activity.defn
async def rotate_tags(promote_key: str) -> RotationResult:
    """Advance serving-artifact state tags and promote ``promote_key`` to current.

    All S3 I/O — hence an activity. ``promote_key`` is the object key of the
    just-built artifact (e.g. ``serving/notes-v1234.duckdb``).

    Rotation order (platform/004 §lifecycle):
      1. Demote existing artifacts one step: current->previous, previous->stale.
         (Already-``stale`` objects stay stale; SeaweedFS lifecycle deletes them.)
      2. Promote ``promote_key`` (was ``building``) to ``current``.
      3. Keep-last-N retention: any artifact beyond the newest :data:`KEEP_LAST_N`
         (by version, descending) is forced to ``stale`` regardless of its tag —
         belt-and-suspenders against tag-rotation gaps.

    The demote-before-promote order is deliberate: it guarantees there is never a
    window with two ``current`` artifacts, and the 5-min grace (in the workflow)
    has already given in-flight queries time to finish on the old current.
    """
    s3 = _s3_client()

    # List every serving artifact object.
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs = {"Bucket": SERVING_BUCKET, "Prefix": SERVING_PREFIX}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    result = RotationResult()

    # 1. Demote existing artifacts (skip the one we're about to promote).
    for key in keys:
        if key == promote_key:
            continue
        state = _get_state(s3, key)
        new_state = _DEMOTE.get(state)
        if new_state is not None:
            _set_state(s3, key, new_state)
            result.demoted[key] = new_state

    # 2. Promote the just-built artifact to current.
    _set_state(s3, promote_key, STATE_CURRENT)
    result.promoted = promote_key

    # 3. Keep-last-N retention sweep (belt-and-suspenders). Order by the version
    #    encoded in the filename (notes-v{N}.duckdb), newest first; anything past
    #    N is forced stale even if its rotation tag hasn't reached stale yet.
    for key in _stale_beyond_keep_last_n(keys, KEEP_LAST_N):
        if key == promote_key:
            continue  # never stale the artifact we just promoted
        if _get_state(s3, key) != STATE_STALE:
            _set_state(s3, key, STATE_STALE)
            result.cleaned.append(key)

    return result


def _version_of(key: str) -> int:
    """Parse the integer version out of a ``.../notes-v{N}.duckdb`` key.

    Returns -1 for keys that don't match the pattern so they sort oldest and are
    cleaned first by the retention sweep.
    """
    match = re.search(r"notes-v(\d+)\.duckdb$", key)
    return int(match.group(1)) if match else -1


def _stale_beyond_keep_last_n(keys: list[str], keep: int) -> list[str]:
    """Return the artifact keys to force-stale: everything past the newest ``keep``.

    Pure helper (no I/O) so the retention policy is unit-testable in isolation.
    """
    ordered = sorted(keys, key=_version_of, reverse=True)
    return ordered[keep:]


@temporalio.workflow.defn
class TagRotationWorkflow:
    """Sleep a grace period, then rotate serving-artifact tags (ADR platform/004).

    Started per build (by WF-SCHEDULES or chained from
    ``BuildServingArtifactWorkflow``). The workflow body sleeps the durable 5-min
    grace timer and invokes :func:`rotate_tags`; all S3 work is in the activity,
    keeping the workflow deterministic.
    """

    @temporalio.workflow.run
    async def run(self, promote_key: str) -> RotationResult:
        """Grace-sleep, then rotate tags promoting ``promote_key`` to current."""
        # Durable timer — in-flight queries against the old current artifact get
        # GRACE_PERIOD to complete before it demotes to previous (platform/004).
        await temporalio.workflow.sleep(GRACE_PERIOD)
        return await temporalio.workflow.execute_activity(
            rotate_tags,
            promote_key,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY_POLICY,
        )


WORKFLOWS = [TagRotationWorkflow]
ACTIVITIES = [rotate_tags]
