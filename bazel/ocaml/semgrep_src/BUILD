# gazelle:ignore
#
# Wave D (Phase 8): the pinned Semgrep CE tree. repositories.bzl clones the
# commit pinned in source.bzl, applies the overlays, and translates the dune
# dirs listed there -> @semgrep_src. The overlays are read by the repository
# rule during fetch, hence the export.

load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("@bazel_skylib//rules:build_test.bzl", "build_test")

exports_files(glob(["overlays/**"]))

bzl_library(
    name = "repositories",
    srcs = [
        "repositories.bzl",
        "source.bzl",
    ],
    visibility = ["//visibility:public"],
)

# Issue #3922's selected native Semgrep CE engine. Keeping the build assertion
# and functional smoke check in this package makes changes to the source pin,
# translator, overlays, or test inputs select both under affected-target CI.
build_test(
    name = "spacegrep_build_test",
    targets = ["@semgrep_src//:spacegrep_exe"],
)

sh_test(
    name = "spacegrep_smoke_test",
    srcs = ["spacegrep_smoke_test.sh"],
    args = [
        "$(rootpath @semgrep_src//:spacegrep_exe)",
        "$(rootpath :spacegrep_smoke_input.txt)",
    ],
    data = [
        ":spacegrep_smoke_input.txt",
        "@semgrep_src//:spacegrep_exe",
    ],
)
