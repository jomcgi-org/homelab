"""Guards on the ENOSPC reclaim gate in the noded rootfs builder (#4458).

The reclaim deletes abandoned build intermediates on a wedged brick, so what is
worth pinning is that it ships disarmed, that its stop condition can actually
evaluate, and above all that it never widens beyond orphans into selecting
among published bases.

Lives in this package rather than in the chart package because a chart package
has no Python toolchain context; it reads the chart files through a
cross-package data dep, the same way cleartext_lane_guard_test reads the deploy
values.
"""

import os
import re
from pathlib import Path

import yaml

DIGITS = re.compile(r"^[0-9]+$")


def _repo_path(*parts: str) -> Path:
    """Resolve a repo-relative path, in-bazel (TEST_SRCDIR) or standalone."""
    rel = Path(*parts)
    candidate = Path(os.environ.get("TEST_SRCDIR", "")) / "_main" / rel
    if candidate.exists():
        return candidate
    # Direct run: this file lives at projects/embervm/runtimes/claude/.
    here = Path(__file__).resolve().parents[4] / rel
    if here.exists():
        return here
    raise FileNotFoundError(f"{rel} not found at {candidate} or {here}")


def _chart_values() -> dict:
    return yaml.safe_load(_repo_path("projects/embervm/chart/values.yaml").read_text())


def test_target_free_bytes_is_a_quoted_string():
    """An unquoted integer here renders in scientific notation and breaks the stop condition.

    Helm promotes a large unquoted YAML integer to a float, so
    `targetFreeBytes: 2147483648` reaches the container as "2.147483648e+09".
    The script compares with `[ "$free" -ge "$target" ]`, which then errors with
    "integer expression expected" and returns 2 rather than a truthy or falsy
    result, so the loop never breaks. In a deletion loop, a stop condition that
    can never fire means delete every candidate instead of stopping once the
    target is met. Keeping the value a QUOTED string is what prevents the
    coercion, so this asserts the YAML type and not just the digits.
    """
    reclaim = _chart_values()["rootfsReclaim"]
    target = reclaim["targetFreeBytes"]
    assert isinstance(target, str), (
        "rootfsReclaim.targetFreeBytes must be a quoted string, got %r (%s). "
        "An unquoted int is rendered by Helm as scientific notation, which the "
        "shell comparison in the rootfs builder cannot parse."
        % (target, type(target).__name__)
    )
    assert DIGITS.match(target), (
        "rootfsReclaim.targetFreeBytes must be plain digits, got %r" % target
    )


def _deploy_values() -> dict:
    return yaml.safe_load(_repo_path("projects/embervm/deploy/values.yaml").read_text())


def _effective(key: str):
    """Chart default, overridden by the deploy overlay when it sets the key.

    The chart default is not what runs. Arming happens in the overlay, so the
    guards below have to judge the effective value, the way cleartext_lane_guard
    reads the deploy values rather than the chart's.
    """
    chart = _chart_values()["rootfsReclaim"]
    deploy = (_deploy_values() or {}).get("rootfsReclaim") or {}
    return deploy[key] if key in deploy else chart[key]


def _size_to_bytes(text: str) -> int:
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    text = str(text).strip()
    if text and text[-1].upper() in units:
        return int(float(text[:-1]) * units[text[-1].upper()])
    return int(text)


def test_target_free_bytes_exceeds_the_bake_size():
    """A target below the rootfs size livelocks the very wedge this exists to break.

    The same constant is BOTH the loop's stop condition and the ENOSPC floor. Set
    it under the bake size and reclaim frees one orphan, stops, fails the retry,
    and on the next restart sees free space already above target and breaks
    before deleting anything. The brick then crashloops forever with reclaimable
    garbage on disk, which is the original incident with extra steps. It also
    truncates the dry-run manifest that arming is decided from.
    """
    target = int(_effective("targetFreeBytes"))
    bake = _size_to_bytes(_chart_values()["rootfsBuilder"]["rootfsSize"])
    assert target > bake, (
        "rootfsReclaim.targetFreeBytes (%d) must exceed rootfsBuilder.rootfsSize "
        "(%s = %d bytes) with headroom for ext4 metadata, or reclaim stops short of "
        "what the retry bake needs and the brick crashloops with garbage still on disk."
        % (target, _chart_values()["rootfsBuilder"]["rootfsSize"], bake)
    )


def test_gate_value_is_a_recognised_setting():
    """Only "" and "1" mean anything; anything else silently reads as disarmed.

    Checks the EFFECTIVE value, so a typo in the deploy overlay (the file where
    arming happens) cannot look armed while behaving as disarmed.
    """
    assert _effective("enabled") in ("", "1")


def test_reclaim_ships_disarmed_by_default():
    """The chart default must never delete. Arming is a deliberate act on deploy values.

    This mirrors how baseRetention was armed: read a live dry-run manifest
    first, then set the gate. A chart that defaulted to armed would delete base
    bundles on any cluster installing it without an override.
    """
    assert _chart_values()["rootfsReclaim"]["enabled"] == ""


def test_reclaim_stays_orphans_only():
    """The reclaim must never select among published bases, only abandoned intermediates.

    A first draft picked the newest base per workload and deleted the rest. It
    was wrong three ways: a `.building` staging orphan looked newest so the LIVE
    base became the candidate, a failed stat won the newest slot so a vanished
    path was retained instead, and equal mtimes resolved by directory order.
    Selecting among published artifacts requires knowing which is current, and
    that is the control plane's job, not a bash script's on a shared hostPath
    with no lock.

    So this asserts the SHAPE of the candidate set rather than any comment: file
    candidates must require both the rootfs- prefix and the .tmp. infix, and
    directory candidates must require the .building suffix. If someone
    reintroduces workload-grouped selection, the negative assertion below fails.
    """
    script = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()

    assert "-name 'rootfs-*.ext4.tmp.*'" in script, (
        "bake-temporary candidates must require BOTH the rootfs- prefix and the "
        ".tmp. infix, so a completed rootfs-<digest>.ext4 can never match"
    )
    assert "-name '*.building'" in script, (
        "staging-orphan candidates must require the .building suffix"
    )
    assert "newest_path" not in script and "newest_mtime" not in script, (
        "the reclaim must not select among published bases by mtime; that is the "
        "rejected design that deleted live bases (see the comment in the script)"
    )


def test_min_age_guard_is_present():
    """Age is the only thing separating an abandoned intermediate from a live one.

    Several brick pods share this hostPath and there is no lock, so a temporary
    a co-located bake is writing right now looks identical to one abandoned
    hours ago. Removing the min-age check would make the reclaim delete another
    pod's in-flight bake.
    """
    script = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()
    assert "EMBERVM_ROOTFS_RECLAIM_MIN_AGE_SECONDS" in script
    assert "$min_age" in script, "the min-age guard is not applied to candidates"
