# gazelle:exclude hello.go
#
# hello.go is the OCaml test's data file (the Go source under parse), not a
# Go package of this repo; without the exclude gazelle generates go_library/
# go_binary targets for it.
load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Phase 8 acceptance: the first source-to-generic-AST pipeline. A real Go
# file parses through the pinned grammar's CST into ast_go and translates
# into the generic AST, asserted structurally via the derived show/eq.
ocaml_test(
    name = "go_generic_test",
    srcs = ["go_generic_test.ml"],
    data = ["hello.go"],
    deps = [
        "@semgrep_src//:ast_generic",
        "@semgrep_src//:parser_go_ast_generic",
        "@semgrep_src//:parser_go_tree_sitter",
    ],
)
