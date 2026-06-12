# Override BUILD for hmap (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. One module.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "hmap",
    srcs = [
        "src/hmap.ml",
        "src/hmap.mli",
    ],
    visibility = ["//visibility:public"],
)
