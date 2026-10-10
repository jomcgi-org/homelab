load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

"""JavaScript/Node.js utilities"""

exports_files(
    ["node_modules_tar.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "node_modules_tar",
    srcs = ["node_modules_tar.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@aspect_rules_js//js:providers"],
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

bzl_library(
    name = "exec_filegroup",
    srcs = ["exec_filegroup.bzl"],
    visibility = ["//visibility:public"],
)
