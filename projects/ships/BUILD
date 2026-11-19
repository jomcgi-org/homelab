load("//bazel/tools/oci:go_image.bzl", "go_image")

go_image(
    name = "image",
    binary = "//projects/ships/cmd",
)

filegroup(
    name = "repo_docs",
    srcs = ["ARCHITECTURE.md"],
    visibility = ["//:__pkg__"],
)
