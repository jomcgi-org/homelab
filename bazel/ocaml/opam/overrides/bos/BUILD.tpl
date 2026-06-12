# Override BUILD for bos (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. Flat module set;
# bos_setup (the bos.setup convenience sublibrary) and the *_top modules are
# not built; nothing here needs them.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "bos",
    srcs = glob(
        [
            "src/*.ml",
            "src/*.mli",
        ],
        exclude = [
            "src/bos_setup*",
            "src/bos_top*",
        ],
    ),
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
    deps = [
        "@ocaml_astring//:astring",
        "@ocaml_fmt//:fmt",
        "@ocaml_fpath//:fpath",
        "@ocaml_logs//:logs",
        "@ocaml_rresult//:rresult",
    ],
)
