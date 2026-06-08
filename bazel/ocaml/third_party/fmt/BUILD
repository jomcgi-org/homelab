load("//bazel/ocaml:defs.bzl", "ocaml_library")

# Vendored `fmt` 0.11.0 (https://erratique.ch/software/fmt, ISC license).
#
# This is the one external opam dependency in the toy. Rather than preinstall it
# in the toolchain image (which would need an `opam install` build step), it is
# vendored as source and compiled through our own ocaml_library — fmt's core
# module is pure OCaml with no C stubs, so it builds standalone against the
# stdlib. opam_deps is left empty: fmt depends only on the stdlib.
#
# See ../../README.md for how the "real" findlib-preinstalled path would look.
ocaml_library(
    name = "fmt",
    srcs = [
        "fmt.ml",
        "fmt.mli",
    ],
    visibility = ["//visibility:public"],
)

exports_files(["LICENSE.md"])
