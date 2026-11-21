load("@rules_oci//oci:defs.bzl", "oci_push")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")

# Publish the layout exercised by the Codex harness version test.
# The HOME-529 runner can be layered on this base.
# gazelle:ignore

alias(
    name = "image",
    actual = "//projects/factory/harness/codex:image",
)

oci_push(
    name = "image.push",
    image = ":image",
    remote_tags = "//projects/factory/harness/codex:image_stamped_tags",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/reviewer/harness",
    visibility = ["//bazel/images:__pkg__"],
)

oci_image_info(
    name = "image.info",
    image = ":image",
    image_digest = "//projects/factory/harness/codex:image.digest",
    image_tags = "//projects/factory/harness/codex:image_stamped_tags",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/reviewer/harness",
    visibility = ["//visibility:public"],
)
