load("//bazel/tools/oci:go_image.bzl", "go_image")

# hf2oci container image — used by oci-model-cache operator as copyImage
go_image(
    name = "image",
    binary = "//bazel/tools/hf2oci/cmd/hf2oci",
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
