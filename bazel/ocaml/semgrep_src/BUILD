# gazelle:ignore
#
# Wave D (Phase 8): the pinned Semgrep CE tree. repositories.bzl clones the
# commit pinned in source.bzl, applies the overlays, and translates the dune
# dirs listed there -> @semgrep_src. The overlays are read by the repository
# rule during fetch, hence the export.

load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

exports_files(glob([
    "overlays/**",
    "patches/**",
]))

bzl_library(
    name = "repositories",
    srcs = [
        "repositories.bzl",
        "source.bzl",
    ],
    visibility = ["//visibility:public"],
)

# Keep direct link assertions and functional smoke checks in this package so
# source pins, translator changes, overlays, and inputs select them under the
# affected-target Linux CI gate.
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

build_test(
    name = "semgrep_core_build_test",
    targets = ["@semgrep_src//:semgrep-core"],
)

sh_test(
    name = "semgrep_core_smoke_test",
    srcs = ["semgrep_core_smoke_test.sh"],
    args = [
        "$(rootpath @semgrep_src//:semgrep-core)",
        "$(rootpath :semgrep_core_smoke_rule.yml)",
        "$(rootpath :semgrep_core_smoke_target.go)",
    ],
    data = [
        ":semgrep_core_smoke_rule.yml",
        ":semgrep_core_smoke_target.go",
        "@semgrep_src//:semgrep-core",
    ],
)
