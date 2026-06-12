# Override BUILD for rresult (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. One module; the
# *_top toplevel-support modules are not built.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "rresult",
    srcs = [
        "src/rresult.ml",
        "src/rresult.mli",
    ],
    visibility = ["//visibility:public"],
)
