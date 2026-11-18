load("//bazel/tools/oci:go_image.bzl", "go_image")

go_image(
    name = "image",
    binary = "//projects/factory-explore/cmd",
)

filegroup(
    name = "repo_docs",
    srcs = [
        "README.md",
        "REVIEW.md",
    ],
    visibility = ["//:__pkg__"],
)
