load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_test")

# Demonstrates depending on a real external opam library (`re`) that our rules
# fetched and built from its own dune file. Depends on re alone (not the vendored
# fmt) because re ships an internal `Fmt` module that would collide at link.
ocaml_binary(
    name = "regex_demo",
    srcs = ["regex_demo.ml"],
    visibility = ["//visibility:public"],
    deps = ["//bazel/ocaml/third_party/re"],
)

build_test(
    name = "regex_demo_build_test",
    targets = [":regex_demo"],
)

ocaml_test(
    name = "regex_test",
    srcs = ["regex_test.ml"],
    deps = ["//bazel/ocaml/third_party/re"],
)
