"""Public entry point for the bazel/ocaml ruleset.

    load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_binary")

See README.md for the toolchain design (digest-pinned OCaml container on RBE).
"""

load(
    "//bazel/ocaml:rules.bzl",
    _OcamlInfo = "OcamlInfo",
    _ocaml_binary = "ocaml_binary",
    _ocaml_library = "ocaml_library",
)

ocaml_library = _ocaml_library
ocaml_binary = _ocaml_binary
OcamlInfo = _OcamlInfo
