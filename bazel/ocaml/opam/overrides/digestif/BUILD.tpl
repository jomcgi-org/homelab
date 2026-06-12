# Override BUILD for digestif (installed by extension.bzl).
# Why an override: digestif is a dune virtual library with two
# implementations; Semgrep's commons selects digestif.ocaml (the pure-OCaml
# baijiu implementation), the plan's decided answer for virtual libraries
# (build the implementation actually selected). The library merges the
# shared src/ support modules with the src-ocaml implementation, exactly
# what dune's (copy_files# ../src/*.ml) does.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "digestif",
    srcs = glob([
        "src-ocaml/*.ml",
    ]) + [
        "src/digestif.mli",
        "src/digestif_bi.ml",
        "src/digestif_by.ml",
        "src/digestif_conv.ml",
        "src/digestif_eq.ml",
    ],
    visibility = ["//visibility:public"],
    deps = ["@ocaml_eqaf//:eqaf"],
)
