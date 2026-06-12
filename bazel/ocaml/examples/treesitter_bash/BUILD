load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Wave D acceptance (Phase 8): the first per-language tree-sitter parser,
# pinned to the commit Semgrep's languages/bash submodule references, parses
# real bash through the typed CST. no-arm64: the grammar's C++ scanner needs
# the Bazel C++ toolchain, which targets x86_64 only in CI (the established
# pattern from examples/treesitter).
ocaml_test(
    name = "ts_bash_test",
    srcs = ["ts_bash_test.ml"],
    tags = ["no-arm64"],
    deps = ["@ocaml_tree_sitter_bash//:tree_sitter_bash"],
)
