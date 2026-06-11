# Override BUILD for the tree-sitter-json grammar (installed by extension.bzl).
#
# Not an opam package: a tree-sitter language grammar, vendored as a cc_library.
# src/parser.c is the generated parser (every tree-sitter grammar ships one);
# it carries its own src/tree_sitter/parser.h. This is the per-language shape
# Semgrep's tree-sitter-lang.* packages repeat grammar by grammar (those with a
# scanner add src/scanner.c to srcs).
load("@rules_cc//cc:defs.bzl", "cc_library")

cc_library(
    name = "tree_sitter_json",
    srcs = glob([
        "src/*.c",
        "src/tree_sitter/*.h",
    ]),
    copts = ["-w"],
    includes = ["src"],
    visibility = ["//visibility:public"],
)
