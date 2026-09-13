# Override BUILD for uutf (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. The library is a
# flat pair of checked-in OCaml modules; its optional top-level and test
# programs are not part of the Semgrep engine closure.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "uutf",
    srcs = [
        "src/uutf.ml",
        "src/uutf.mli",
    ],
    visibility = ["//visibility:public"],
)
