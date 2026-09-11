load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Wave D acceptance (Phase 8): the first per-language tree-sitter parser,
# pinned to the commit Semgrep's languages/bash submodule references, parses
# real bash through the typed CST, including the grammar's C++ scanner.
ocaml_test(
    name = "ts_bash_test",
    srcs = ["ts_bash_test.ml"],
    deps = ["@ocaml_tree_sitter_bash//:tree_sitter_bash"],
)
