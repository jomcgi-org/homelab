load("//bazel/helm:defs.bzl", "helm_chart")

# Hot git mirror chart. The single image (git-daemon + refresh loop) is pinned
# at build time from the image's .info provider.
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/agent_platform/git-mirror:image.info",
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
