# Override BUILD for fpath (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate (the cmdliner/logs
# pattern). One module, deps astring.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "fpath",
    srcs = [
        "src/fpath.ml",
        "src/fpath.mli",
    ],
    visibility = ["//visibility:public"],
    deps = ["@ocaml_astring//:astring"],
)
