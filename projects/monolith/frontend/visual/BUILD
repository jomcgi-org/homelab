# gazelle:ignore
#
# Chromium exec image for the hermetic visual-regression capture (ADR
# tooling/010). This is a CI-execution-only image, published under the ci/
# GHCR namespace (not a deployed runtime artifact), used as the RBE exec
# environment (exec_properties.container-image) for the playwright capture so
# chromium has its shared-library runtime + fonts. The browser binary and node
# come from the hermetic Bazel toolchain / rules_playwright, not this image.
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

apko_image(
    name = "visual_chromium",
    config = "apko.yaml",
    contents = "@visual_chromium_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/ci/visual-chromium",
)
