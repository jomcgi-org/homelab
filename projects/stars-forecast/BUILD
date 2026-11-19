load("//bazel/tools/oci:go_image.bzl", "go_image")

go_image(
    name = "image",
    binary = "//projects/scotland-connector/cmd/stars-forecast",
)

filegroup(
    name = "repo_docs",
    srcs = [
        "ARCHITECTURE.md",
        "README.md",
    ],
    visibility = ["//:__pkg__"],
)
