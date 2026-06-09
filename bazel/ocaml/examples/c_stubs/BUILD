load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library", "ocaml_test")

# An ocaml_library whose implementation is partly a C stub (dune's
# `foreign_stubs`/`c_names`). The .c is compiled by ocamlopt and folded into
# counter.a, so anything that links :counter resolves the `external` with no
# extra link wiring.
ocaml_library(
    name = "counter",
    srcs = [
        "counter.ml",
        "counter.mli",
    ],
    c_srcs = ["counter_stubs.c"],
    visibility = ["//visibility:public"],
)

ocaml_binary(
    name = "popcount_demo",
    srcs = ["popcount_demo.ml"],
    visibility = ["//visibility:public"],
    deps = [":counter"],
)

build_test(
    name = "popcount_demo_build_test",
    targets = [":popcount_demo"],
)

ocaml_test(
    name = "counter_test",
    srcs = ["counter_test.ml"],
    deps = [":counter"],
)
