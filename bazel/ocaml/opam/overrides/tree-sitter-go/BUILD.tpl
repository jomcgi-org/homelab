# Override BUILD for tree-sitter-lang.go (installed by extension.bzl).
#
# Not an opam release: semgrep-go pinned to the commit Semgrep's languages/go
# submodule references (lock version = short sha), the tree-sitter-bash
# pattern. Go's grammar has no external scanner, so the cc_library is just
# the generated parser.c against the vendored tree-sitter runtime
# (includes = ["lib"] resolves the repo-local tree_sitter/parser.h copy).
# The OCaml binding stub (bindings.c) rides c_srcs with the archives via
# cc_deps. cc_library means x86_64-only in CI (the established no-arm64
# pattern); consumers tag accordingly.
load("@rules_cc//cc:defs.bzl", "cc_library")
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

cc_library(
    name = "go_grammar",
    srcs = ["lib/parser.c"] + glob(["lib/tree_sitter/*.h"]),
    copts = ["-w"],
    includes = ["lib"],
    visibility = ["//visibility:public"],
    deps = ["@ocaml_tree_sitter_c//:tree_sitter"],
)

ocaml_library(
    name = "tree_sitter_go",
    srcs = [
        "lib/Boilerplate.ml",
        "lib/CST.ml",
        "lib/Parse.ml",
        "lib/Parse.mli",
    ],
    c_srcs = ["lib/bindings.c"],
    cc_deps = [
        ":go_grammar",
        "@ocaml_tree_sitter_c//:tree_sitter",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = ["@ocaml_tree_sitter_core//:tree_sitter_run"],
)
