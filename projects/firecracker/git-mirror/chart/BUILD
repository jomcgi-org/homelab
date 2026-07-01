load("//bazel/helm:defs.bzl", "helm_chart")

# Hot git mirror chart. The single image (bash + git + git-daemon from Wolfi)
# is pinned at build time from the image's .info provider via helm_images_values.
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/firecracker/git-mirror:image.info",
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
