"""Providers for the buck2 OCI rules.

Buck2 counterpart to bazel/tools/oci/providers.bzl's OciImageInfo. Carries the
target registry repository and a file holding the image's content digest
(sha256:...), so helm_images_values can pin chart image references by digest —
the deterministic analogue of bazel's stamped-tag injection.
"""

OciImageInfo = provider(
    # repository: str registry URL (e.g. "ghcr.io/jomcgi/homelab/rust-service")
    # digest: artifact (a file containing "sha256:<hex>")
    fields = ["repository", "digest"],
)
