load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_test")

# tree-sitter end to end: the vendored runtime + the json grammar (both
# cc_libraries) bound through an OCaml C stub via cc_deps, parsing real JSON.
# This is the exact shape Semgrep's per-language parsers need (a grammar
# cc_library per language next to the shared runtime).
ocaml_library(
    name = "ts_json",
    srcs = ["ts_json.ml"],
    c_srcs = ["ts_stubs.c"],
    cc_deps = [
        "@ocaml_tree_sitter_c//:tree_sitter",
        "@ocaml_tree_sitter_json//:tree_sitter_json",
    ],
)

ocaml_test(
    name = "ts_test",
    srcs = ["ts_test.ml"],
    deps = [":ts_json"],
)
