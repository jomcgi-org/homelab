# Override BUILD for integers (installed by extension.bzl).
#
# Why an override: the dune stanza uses (install_c_headers ocaml_integers) and
# (c_names unsigned_stubs), the pre-foreign_stubs spelling for C stubs, which
# dune2bazel does not model. The build itself is one flat library: the stub
# compiles via c_srcs and the header stages via c_headers (the parmap shape).
#
# ocaml_integers.h is exported because ctypes' dune copies it next to its own
# sources (%{lib:integers:ocaml_integers.h}) and installs it; the ctypes
# override reproduces that copy.
#
# integers.top (toplevel pretty printers, byte-only over compiler-libs) stays
# unbuilt: nothing in the universe names it.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

exports_files(["src/ocaml_integers.h"])

ocaml_library(
    name = "integers",
    srcs = glob([
        "src/*.ml",
        "src/*.mli",
    ]),
    c_headers = ["src/ocaml_integers.h"],
    c_srcs = ["src/unsigned_stubs.c"],
    visibility = ["//visibility:public"],
    deps = ["@ocaml_stdlib_shims//:stdlib_shims"],
)
