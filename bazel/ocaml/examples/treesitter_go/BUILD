load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Phase 8 acceptance: the second per-language tree-sitter parser (go, the
# lightest lib_parsing_tree_sitter consumer: its glue needs only
# parser_go.ast, where python's also pulls ast_generic), pinned to the
# commit Semgrep's languages/go submodule references. no-arm64: the grammar
# builds as a cc_library, and the Bazel C++ toolchain targets x86_64 only
# in CI (the established pattern from examples/treesitter).
ocaml_test(
    name = "ts_go_test",
    srcs = ["ts_go_test.ml"],
    tags = ["no-arm64"],
    deps = ["@ocaml_tree_sitter_go//:tree_sitter_go"],
)
