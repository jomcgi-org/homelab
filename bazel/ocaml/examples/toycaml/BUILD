load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library", "ocaml_test")

# tOyCaml -- a deliberately tiny "grep for code" that mirrors the *shape* of how
# a large OCaml analysis engine uses the ecosystem, so the ruleset can be grown
# against a representative target rather than a generic hello-world. See README.md
# and docs/decisions/tooling/005-toycaml-demonstrator.md.
#
# The BUILD files here are hand-written today; they are the first acceptance
# corpus for the OCaml BUILD generator decided in
# docs/decisions/tooling/007-ocaml-build-file-generation-gazelle.md -- the
# generator must reproduce them from the sources alone.

# A small, hot helper pushed into C (FNV-1a string hash), bound via `external`.
# Mirrors the lone first-party C foreign-stub such an engine keeps. Exercises the
# c_srcs path (same as examples/c_stubs): the .c is folded into the library .a.
ocaml_library(
    name = "toycaml_intern",
    srcs = [
        "intern.ml",
        "intern.mli",
    ],
    c_srcs = ["intern_stubs.c"],
    visibility = ["//visibility:public"],
)

# The matching engine: a generic AST, a hand-written lexer/parser, a pattern type
# with metavariables, and a structural matcher. Exercises a multi-module whole-
# library compile with real inter-module ordering (ocamldep -sort), .mli
# interfaces, an inter-library dep (:toycaml_intern), and a real fetched-from-
# source opam dependency (`re`, used to validate identifier tokens). Depends on
# `re` alone -- not the vendored `fmt` -- because re ships an internal `Fmt`
# module that would collide at link until library wrapping lands.
ocaml_library(
    name = "toycaml_lib",
    srcs = [
        "ast.ml",
        "ast.mli",
        "lexer.ml",
        "lexer.mli",
        "matcher.ml",
        "matcher.mli",
        "parse.ml",
        "parse.mli",
        "pattern.ml",
        "pattern.mli",
    ],
    visibility = ["//visibility:public"],
    deps = [
        ":toycaml_intern",
        "//bazel/ocaml/third_party/re",
    ],
)

# The osemgrep-shaped CLI entry point.
ocaml_binary(
    name = "toycaml",
    srcs = ["main.ml"],
    visibility = ["//visibility:public"],
    deps = [":toycaml_lib"],
)

# Assert the binary links (the link path, without running it).
build_test(
    name = "toycaml_build_test",
    targets = [":toycaml"],
)

# Exercises the full stack end to end: lex, parse, match, and the C stub.
ocaml_test(
    name = "toycaml_test",
    srcs = ["matcher_test.ml"],
    deps = [":toycaml_lib"],
)
