load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Phase 8 acceptance: the second per-language tree-sitter parser (go, the
# lightest lib_parsing_tree_sitter consumer: its glue needs only
# parser_go.ast, where python's also pulls ast_generic), pinned to the
# commit Semgrep's languages/go submodule references.
ocaml_test(
    name = "ts_go_test",
    srcs = ["ts_go_test.ml"],
    deps = ["@ocaml_tree_sitter_go//:tree_sitter_go"],
)
