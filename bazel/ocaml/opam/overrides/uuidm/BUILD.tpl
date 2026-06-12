# Override BUILD for uuidm (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. One module.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "uuidm",
    srcs = [
        "src/uuidm.ml",
        "src/uuidm.mli",
    ],
    visibility = ["//visibility:public"],
)
