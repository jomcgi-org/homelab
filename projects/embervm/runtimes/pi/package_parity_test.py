"""Guard: the pi runtime's wolfi package set matches the claude runtime's.

The pi image exists to drop two CLI tars (claude and codex, 200 MB of the
claude runtime's 300 MB compressed), NOT to hand-trim the wolfi base, which is
59.7 MB of that total. Keeping the package sets identical is what makes this
image a behavioural drop-in for a pi turn: the same git, gh, curl and jq are
present, so the only difference between the two guests is which CLIs exist.

Without this test the claim is a comment. A package added to the claude runtime
and not here would surface as a pi turn missing a binary the claude runtime has,
inside a guest, which is the most expensive place to find out.

Divergence is allowed. It just has to be deliberate: add an entry to
EXPECTED_DIVERGENCE with the reason, which makes the difference reviewable in
the diff rather than discoverable in production.
"""

from pathlib import Path

import yaml

# Package -> why it is in one image and not the other. Empty today: the sets are
# identical on purpose (see apko.yaml). A future entry reads like
#   "gh": "pi's 4-tool loop never opens a PR, and gh is ~20 MB",
EXPECTED_DIVERGENCE: dict[str, str] = {}


def _packages(path: Path) -> set[str]:
    config = yaml.safe_load(path.read_text())
    return set(config["contents"]["packages"])


def _runfile(relative: str) -> Path:
    """Resolve a repo-relative path from the test's runfiles."""
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        hit = candidate / relative
        if hit.exists():
            return hit
    raise AssertionError(f"{relative} not found in runfiles under {here}")


def test_package_sets_match_the_claude_runtime():
    pi = _packages(_runfile("projects/embervm/runtimes/pi/apko.yaml"))
    claude = _packages(_runfile("projects/embervm/runtimes/claude/apko.yaml"))

    only_pi = pi - claude - set(EXPECTED_DIVERGENCE)
    only_claude = claude - pi - set(EXPECTED_DIVERGENCE)

    assert not only_pi, (
        f"packages in the pi runtime but not the claude runtime: {sorted(only_pi)}. "
        "Add them to the claude runtime too, or record the reason in "
        "EXPECTED_DIVERGENCE."
    )
    assert not only_claude, (
        f"packages in the claude runtime but not the pi runtime: {sorted(only_claude)}. "
        "A pi session reaches for the same tools a claude session does, so add "
        "them here too, or record the reason in EXPECTED_DIVERGENCE."
    )


def test_pi_runtime_is_amd64_only():
    """An aarch64 arch here without a per-arch guest-init tar fails at PUSH time.

    The claude runtime records the same constraint. It is asserted rather than
    only commented because the failure is a missing layer blob at push, long
    after PR CI has gone green (see the arm64 note in bazel/tools/oci).
    """
    config = yaml.safe_load(
        _runfile("projects/embervm/runtimes/pi/apko.yaml").read_text()
    )
    assert config["archs"] == ["x86_64"], (
        "the pi runtime is amd64-only; adding aarch64 needs a per-arch "
        "guest-init tar and arm64 = True in BUILD, together"
    )
