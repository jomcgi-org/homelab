load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Phase 9 wave 7 acceptance: the jsonnet tree-sitter parser, the grammar
# libs/ojsonnet consumes (via parser_jsonnet.tree_sitter). Unlike go, the
# jsonnet grammar carries an external scanner (lib/scanner.c), so the
# cc_library compiles parser.c plus scanner.c; this test drives a snippet
# through the generated Parse module to prove the scanner links and the CST
# comes back. no-arm64: the grammar builds as a cc_library, and the Bazel
# C++ toolchain targets x86_64 only in CI (the established pattern from
# examples/treesitter_go).
ocaml_test(
    name = "ts_jsonnet_test",
    srcs = ["ts_jsonnet_test.ml"],
    tags = ["no-arm64"],
    deps = ["@ocaml_tree_sitter_jsonnet//:tree_sitter_jsonnet"],
)
