load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library", "ocaml_test")

# Three modules with a real inter-module dependency (greeting -> message), so
# `ocamldep -sort` has ordering work to do.
ocaml_library(
    name = "hello_lib",
    srcs = [
        "greeting.ml",
        "greeting.mli",
        "message.ml",
    ],
)

# Native executable: links the local library, the vendored `fmt` opam dep, and
# the `unix` findlib package (opam_deps).
ocaml_binary(
    name = "hello",
    srcs = ["main.ml"],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
    deps = [
        ":hello_lib",
        "//bazel/ocaml/third_party/fmt",
    ],
)

# Idiomatic OCaml test: a native executable that exits non-zero on failure.
# `bazel test //...` runs it directly (no wrapper). Picked up by the global
# test-all with no extra wiring.
ocaml_test(
    name = "greeting_test",
    srcs = ["test_greeting.ml"],
    data = ["testdata/expected.txt"],
    deps = [
        ":hello_lib",
        "//bazel/ocaml/third_party/fmt",
    ],
)

# Also assert the runnable binary itself links (covers the unix opam_deps path
# that the library test doesn't exercise).
build_test(
    name = "hello_build_test",
    targets = [":hello"],
)
