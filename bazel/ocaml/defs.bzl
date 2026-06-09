"""Public entry point for the bazel/ocaml ruleset.

    load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_binary")

See README.md for the toolchain design (Semgrep's OCaml 5.3 fork, built from
source into a relocatable sysroot staged as action inputs).
"""

load(
    "//bazel/ocaml:rules.bzl",
    _OcamlInfo = "OcamlInfo",
    _ocaml_binary = "ocaml_binary",
    _ocaml_library = "ocaml_library",
    _ocaml_test = "ocaml_test",
)

ocaml_library = _ocaml_library
ocaml_binary = _ocaml_binary
ocaml_test = _ocaml_test
OcamlInfo = _OcamlInfo
