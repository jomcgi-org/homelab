# Override BUILD for eqaf (installed by extension.bzl).
# Why an override: a config executable picks the unsafe primitives variant by
# compiler version ((rule) copying %{read:which-unsafe-file}); on any
# OCaml >= 4.08 that is unsafe_stable.ml, so the choice is pinned. The
# bigstring/bytes/cstruct sublibraries are not built; digestif needs core
# eqaf only.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "unsafe_ml",
    srcs = ["lib/unsafe_stable.ml"],
    outs = ["unsafe.ml"],
    cmd = "cp $(location lib/unsafe_stable.ml) $@",
)

ocaml_library(
    name = "eqaf",
    srcs = [
        "lib/eqaf.ml",
        "lib/eqaf.mli",
        ":unsafe_ml",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
)
