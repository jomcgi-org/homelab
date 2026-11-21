from unittest.mock import patch

import pytest

import private_harness_package as guard

REPO = "ghcr.io/jomcgi/homelab/projects/factory/harness/codex"


@pytest.mark.parametrize(
    "repository",
    [
        "ghcr.io/jomcgi/homelab/projects/factory/harness/claude",
        REPO,
        "ghcr.io/jomcgi/homelab/projects/factory/reviewer/harness",
    ],
)
def test_existing_private_package_does_not_bootstrap(repository):
    with (
        patch.object(guard, "package_visibility", return_value="private") as read,
        patch.object(guard, "seed_package") as seed,
    ):
        guard.verify_private(repository, "test-token", "crane")
    read.assert_called_once_with(repository, "test-token")
    seed.assert_not_called()


@pytest.mark.parametrize("visibility", ["public", "internal", "unknown"])
def test_nonprivate_package_stops_publication(visibility):
    with (
        patch.object(guard, "package_visibility", return_value=visibility),
        patch.object(guard, "seed_package") as seed,
        pytest.raises(guard.PrivacyError, match="no verified private visibility"),
    ):
        guard.verify_private(REPO, "test-token", "crane")
    seed.assert_not_called()


@pytest.mark.parametrize("visibility", ["private", "public"])
def test_new_package_is_checked_again_after_the_harmless_push(visibility):
    with (
        patch.object(guard, "package_visibility", side_effect=[None, None, visibility]),
        patch.object(guard, "seed_package") as seed,
        patch.object(guard.time, "sleep"),
    ):
        if visibility == "private":
            guard.verify_private(REPO, "test-token", "crane")
        else:
            with pytest.raises(
                guard.PrivacyError, match="no verified private visibility"
            ):
                guard.verify_private(REPO, "test-token", "crane")
    seed.assert_called_once_with(REPO, "crane")


def test_failed_metadata_read_never_bootstraps():
    with (
        patch.object(
            guard, "package_visibility", side_effect=guard.PrivacyError("HTTP 403")
        ),
        patch.object(guard, "seed_package") as seed,
        pytest.raises(guard.PrivacyError, match="HTTP 403"),
    ):
        guard.verify_private(REPO, "test-token", "crane")
    seed.assert_not_called()
