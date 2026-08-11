"""Guard: the packaged homelab-library must deploy images by content digest.

``helm_images_values`` emits ``repository``, ``tag`` and ``digest`` for every
Bazel-built image, and the tag is build-timestamped, so it moves on every commit
to main even when the image bytes are identical. ``push-changed.sh`` skips
pushing an image whose content digest is already in the registry, so that new
tag is frequently never created. A chart that deployed ``repository:tag``
therefore pinned a tag that does not exist, which is an ImagePullBackOff: that
is what wedged monolith-public's rollout on 2026-08-11 (chart 0.287.0, commit
95eb93de7, where both public images were content-identical so neither push ran).

This reads the PACKAGED tarball rather than the library source on purpose. The
tarball under ``charts/`` is what Bazel globs and ships, and ``sync-helm-deps.sh``
only rebuilds it when the library *version* changes, never on content. So a
source fix that was never re-synced would look correct in the diff and deploy
the old template. Asserting on the shipped artifact closes that gap.

The templates are Go-templated (not valid YAML on their own), so the assertions
are over the raw template text.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

# Templates that render a container image and must route through the helper.
_IMAGE_RENDERING_TEMPLATES = ("_deployment.tpl", "_statefulset.tpl")


def _packaged_library() -> Path:
    chart_dir = Path("projects/monolith-public/chart/charts")
    roots = [
        Path(__file__).resolve().parents[2],
        Path(os.environ.get("TEST_SRCDIR", "")) / "_main",
    ]
    for root in roots:
        matches = sorted((root / chart_dir).glob("homelab-library-*.tgz"))
        if matches:
            # Exactly one is expected; a second means a stale version was left
            # behind by a partial sync, and the guard should not pick blindly.
            assert len(matches) == 1, f"expected one packaged library, found {matches}"
            return matches[0]
    raise FileNotFoundError(
        f"homelab-library-*.tgz not found under any of {roots} "
        f"(TEST_SRCDIR={os.environ.get('TEST_SRCDIR', '')!r})"
    )


def _library_template(name: str) -> str:
    with tarfile.open(_packaged_library()) as tar:
        member = tar.extractfile(f"homelab-library/templates/{name}")
        assert member is not None, f"{name} missing from the packaged library"
        return member.read().decode()


def test_image_rendering_templates_use_the_shared_helper():
    for name in _IMAGE_RENDERING_TEMPLATES:
        text = _library_template(name)
        assert 'include "homelab.imageRef"' in text, (
            f"{name} must render its image through homelab.imageRef, "
            f"which prefers the content digest over the build-timestamped tag"
        )
        assert "{{ $vals.image.repository }}:{{ $vals.image.tag }}" not in text, (
            f"{name} renders a bare tag reference. The build-timestamped tag is "
            f"frequently never pushed (push-changed.sh skips content-identical "
            f"images), so this deploys a tag that does not exist."
        )


def test_helper_prefers_digest_and_falls_back_to_tag():
    text = _library_template("_helpers.tpl")

    assert '{{- define "homelab.imageRef" -}}' in text, (
        "homelab.imageRef is missing from the packaged library; the image "
        "templates include it, so rendering would fail"
    )

    body = text.split('{{- define "homelab.imageRef" -}}', 1)[1].split("{{- end }}", 1)[
        0
    ]

    # The digest branch must come first, so a digest always wins when present.
    assert body.index("@{{ .digest }}") < body.index(":{{ .tag }}"), (
        "homelab.imageRef must prefer the digest; the tag is only a fallback "
        "for upstream images that never pass through helm_images_values"
    )
    # The tag fallback is load-bearing: imgproxy and other upstream images carry
    # no digest key and would render "repo:<no value>" without it.
    assert ":{{ .tag }}" in body, (
        "homelab.imageRef must keep the tag fallback for images with no digest"
    )
