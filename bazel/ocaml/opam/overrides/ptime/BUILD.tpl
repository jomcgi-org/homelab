# Override BUILD for ptime (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. One module; the
# clock and toplevel sublibraries are not built (timedesc needs ptime only).
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "ptime",
    srcs = [
        "src/ptime.ml",
        "src/ptime.mli",
    ],
    visibility = ["//visibility:public"],
)
