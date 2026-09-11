load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@rules_cc//cc:defs.bzl", "cc_library")

# Phase 6 probe: confirm a C/C++ toolchain resolves and compiles a cc_library on
# the BuildBuddy RBE executor (the prerequisite for delegating OCaml C stubs to
# cc_library). If this is green, the ocaml_library `cc_deps` integration can
# rely on cc_library producing executor-linkable static archives.
cc_library(
    name = "answer",
    srcs = ["answer.c"],
    hdrs = ["answer.h"],
    visibility = ["//visibility:public"],
)

build_test(
    name = "answer_build_test",
    targets = [":answer"],
)
