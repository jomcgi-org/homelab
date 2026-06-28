load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Hot git mirror for Firecracker agent workspaces (goosecracker). Keeps bare
# --mirror clones of registered repos warm on node-4's NVMe so guests can
# fetch their workspace from a nearby always-fresh mirror instead of cloning
# GitHub from scratch each turn.
apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@git_mirror_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/agent_platform/git-mirror",
)
