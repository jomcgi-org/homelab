load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Hot git mirror for Firecracker agent workspaces (goosecracker). Keeps bare
# clones of registered repos warm on node-4 so guests can fetch their workspace
# from a nearby always-fresh mirror instead of cloning GitHub from scratch each
# turn. Serves git:// (port 9418) via git-daemon; a supervisor loop refreshes
# each repo on a configurable interval and enforces the refs/agents/** write
# boundary via a pre-receive hook.
apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@git_mirror_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/firecracker/git-mirror",
)
