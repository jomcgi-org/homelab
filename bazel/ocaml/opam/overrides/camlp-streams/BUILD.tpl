# Override BUILD for camlp-streams (installed by extension.bzl).
#
# Why an override: upstream's dune is a jbuild_plugin OCaml program that picks
# how to build per compiler version. On OCaml 5.0+ (this toolchain is 5.3) it
# simply compiles src/*.{ml,mli} (Stream + Genlex, the modules the stdlib
# dropped in 5.0) into a `wrapped false` library with -w -9.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "camlp_streams",
    srcs = glob(["src/*.ml", "src/*.mli"]),
    ocamlopt_flags = [
        "-w",
        "-9",
    ],
    visibility = ["//visibility:public"],
)
