load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Prebuilt TLC model checker for the EmberVM TLA+ pilot (ADR embervm/006).
# See repositories.bzl for why a prebuilt jar + vendored JRE (not a hermetic
# rules_java toolchain) are used here.

# The tlc.sh driver is consumed by the //projects/embervm/specs TLC genrules
# (the pilot spec checks). No smoke genrule lives here: those spec checks are the
# smoke that exercises this toolchain end to end.
exports_files(
    ["tlc.sh"],
    visibility = ["//projects/embervm:__subpackages__"],
)

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@bazel_tools//tools/build_defs/repo:http.bzl"],
)
