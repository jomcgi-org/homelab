# Override BUILD for pcre-ocaml (installed by extension.bzl).
# Why an override: upstream's dune uses dune-configurator to find a system
# libpcre via pkg-config; the vendored PCRE1 cc_library links through cc_deps
# instead (the pcre2 pattern).
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "pcre",
    srcs = [
        "lib/pcre.ml",
        "lib/pcre.mli",
    ],
    c_srcs = ["lib/pcre_stubs.c"],
    cc_deps = ["@ocaml_pcre1_c//:pcre1"],
    visibility = ["//visibility:public"],
)
