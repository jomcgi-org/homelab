# Override BUILD for tree-sitter-lang.jsonnet (installed by extension.bzl).
#
# Not an opam release: semgrep-jsonnet pinned to the commit Semgrep's
# languages/jsonnet/tree-sitter submodule references (lock version = short
# sha), the tree-sitter-go/-bash pattern. Unlike go, the jsonnet grammar HAS
# an external scanner (lib/scanner.c, plain C here, not the C++ scanner.cc
# bash ships), so the cc_library compiles parser.c AND scanner.c against the
# vendored tree-sitter runtime (includes = ["lib"] resolves the repo-local
# tree_sitter/parser.h copy). The OCaml binding stub (bindings.c) rides
# c_srcs with the archives via cc_deps. cc_library means x86_64-only in CI
# (the established no-arm64 pattern); consumers tag accordingly.
load("@rules_cc//cc:defs.bzl", "cc_library")
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

cc_library(
    name = "jsonnet_grammar",
    srcs = [
        "lib/parser.c",
        "lib/scanner.c",
    ] + glob(["lib/tree_sitter/*.h"]),
    copts = ["-w"],
    includes = ["lib"],
    visibility = ["//visibility:public"],
    deps = ["@ocaml_tree_sitter_c//:tree_sitter"],
)

ocaml_library(
    name = "tree_sitter_jsonnet",
    srcs = [
        "lib/Boilerplate.ml",
        "lib/CST.ml",
        "lib/Parse.ml",
        "lib/Parse.mli",
    ],
    c_srcs = ["lib/bindings.c"],
    cc_deps = [
        ":jsonnet_grammar",
        "@ocaml_tree_sitter_c//:tree_sitter",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = ["@ocaml_tree_sitter_core//:tree_sitter_run"],
)
