load("//bazel/tools/oci:go_image.bzl", "go_image")

go_image(
    name = "image",
    binary = "//projects/scotland-connector/cmd/hikes-writer",
)

filegroup(
    name = "repo_docs",
    srcs = [
        "ARCHITECTURE.md",
        "README.md",
    ],
    visibility = ["//projects/monolith:__pkg__"],
)
