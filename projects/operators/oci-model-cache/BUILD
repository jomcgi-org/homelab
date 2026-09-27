load("//bazel/tools/oci:go_image.bzl", "go_image")

# OCI Model Cache operator container image
go_image(
    name = "image",
    binary = "//projects/operators/oci-model-cache/cmd",
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
