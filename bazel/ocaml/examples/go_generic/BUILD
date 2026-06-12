load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Phase 8 acceptance: the first source-to-generic-AST pipeline. A real Go
# file parses through the pinned grammar's CST into ast_go and translates
# into the generic AST, asserted structurally via the derived show/eq.
# no-arm64: the grammar builds as a cc_library, and the Bazel C++ toolchain
# targets x86_64 only in CI (the established pattern from examples/treesitter).
ocaml_test(
    name = "go_generic_test",
    srcs = ["go_generic_test.ml"],
    data = ["hello.go"],
    tags = ["no-arm64"],
    deps = [
        "@semgrep_src//:ast_generic",
        "@semgrep_src//:parser_go_ast_generic",
        "@semgrep_src//:parser_go_tree_sitter",
    ],
)
