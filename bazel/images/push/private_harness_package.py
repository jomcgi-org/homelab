"""Require private GHCR package metadata before a harness image upload."""

import argparse
import json
import os
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HARNESS_PACKAGES = frozenset(
    {
        "ghcr.io/jomcgi/homelab/projects/factory/harness/claude",
        "ghcr.io/jomcgi/homelab/projects/factory/harness/codex",
    }
)


class PrivacyError(Exception):
    """The package's current privacy could not be verified."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the registry credential on the configured GitHub API host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def package_visibility(repository, token):
    package = urllib.parse.quote(repository.removeprefix("ghcr.io/jomcgi/"), safe="")
    request = urllib.request.Request(
        f"https://api.github.com/users/jomcgi/packages/container/{package}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=10
        ) as reply:
            metadata = json.load(reply)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise PrivacyError(
            f"GitHub package metadata returned HTTP {exc.code}"
        ) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise PrivacyError("GitHub package metadata could not be read") from None
    expected_name = repository.removeprefix("ghcr.io/jomcgi/")
    if not isinstance(metadata, dict) or metadata.get("name") != expected_name:
        raise PrivacyError("GitHub returned metadata for an unexpected package")
    return metadata.get("visibility") or "unknown"


def seed_package(repository, crane):
    # Make the first version from scratch. Neither a base image nor any files
    # from the harness build enter this placeholder.
    with tempfile.TemporaryDirectory() as directory:
        empty = Path(directory) / "empty.tar"
        with tarfile.open(empty, mode="w"):
            pass
        try:
            subprocess.run(
                [
                    crane,
                    "append",
                    "--oci-empty-base",
                    "--new_layer",
                    str(empty),
                    "--new_tag",
                    f"{repository}:privacy-bootstrap",
                ],
                check=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            raise PrivacyError(
                "The harmless bootstrap image could not be uploaded"
            ) from None


def verify_private(repository, token, crane):
    if repository not in HARNESS_PACKAGES:
        raise PrivacyError("Unknown private harness destination")
    if not token:
        raise PrivacyError("GHCR_TOKEN is required to verify package privacy")
    visibility = package_visibility(repository, token)
    if visibility is None:
        seed_package(repository, crane)
        # The registry may acknowledge the write before metadata is indexed.
        for delay in (0, 1, 2, 4, 8):
            if delay:
                time.sleep(delay)
            visibility = package_visibility(repository, token)
            if visibility is not None:
                break
    if visibility != "private":
        raise PrivacyError("The destination has no verified private visibility")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", choices=sorted(HARNESS_PACKAGES))
    parser.add_argument("crane")
    args = parser.parse_args()
    try:
        verify_private(args.repository, os.environ.get("GHCR_TOKEN"), args.crane)
    except PrivacyError as exc:
        parser.exit(1, f"ERROR: {args.repository}: {exc}\n")
    print(f"Verified private GHCR package: {args.repository}")


if __name__ == "__main__":
    main()
