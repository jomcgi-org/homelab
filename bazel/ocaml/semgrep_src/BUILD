# gazelle:ignore
#
# Wave D (Phase 8): the pinned Semgrep CE tree. repositories.bzl clones the
# commit pinned in source.bzl, applies the overlays, and translates the dune
# dirs listed there -> @semgrep_src. The overlays are read by the repository
# rule during fetch, hence the export.

load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

exports_files(glob(["overlays/**"]))

bzl_library(
    name = "repositories",
    srcs = [
        "repositories.bzl",
        "source.bzl",
    ],
    visibility = ["//visibility:public"],
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
