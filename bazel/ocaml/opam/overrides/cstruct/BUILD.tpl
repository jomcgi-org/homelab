# Override BUILD for cstruct (installed by extension.bzl).
# Why an override: the dune library carries (foreign_stubs), a (modules ...)
# filter (cstruct_sexp is a separate sublibrary nothing here needs), and a
# js_of_ocaml field. The core is cstruct + cstruct_cap with one C stub.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "cstruct",
    srcs = [
        "lib/cstruct.ml",
        "lib/cstruct.mli",
        "lib/cstruct_cap.ml",
        "lib/cstruct_cap.mli",
    ],
    c_srcs = ["lib/cstruct_stubs.c"],
    visibility = ["//visibility:public"],
)
