load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library", "ocaml_test")

# tOyCaml -- a deliberately tiny "grep for code" that mirrors the *shape* of how
# a large OCaml analysis engine uses the ecosystem, so the ruleset can be grown
# against a representative target rather than a generic hello-world. See README.md
# and bazel/ARCHITECTURE.md (OCaml section, ADR map: tooling/005).
#
# The BUILD files here are hand-written today; they are the first acceptance
# corpus for the OCaml BUILD generator decided in
# bazel/ARCHITECTURE.md (ADR map: tooling/007) -- the
# generator must reproduce them from the sources alone.
#
# Module files are tc_-prefixed deliberately. The ruleset has no library
# wrapping yet (ADR 004 Phase 2), and `re` -- built unwrapped -- exposes flat
# `Ast`/`Fmt`/`Str` units; an unprefixed `Ast` here would collide with re's at
# link. Manual prefixing is the stand-in for dune-style wrapping, and tOyCaml
# hitting this is exactly why wrapping is on the roadmap.

# A small, hot helper pushed into C (FNV-1a string hash), bound via `external`.
# Mirrors the lone first-party C foreign-stub such an engine keeps. Exercises the
# c_srcs path (same as examples/c_stubs): the .c is folded into the library .a.
ocaml_library(
    name = "toycaml_intern",
    srcs = [
        "tc_intern.ml",
        "tc_intern.mli",
    ],
    c_srcs = ["intern_stubs.c"],
    visibility = ["//visibility:public"],
)

# The matching engine: a generic AST, a hand-written lexer/parser, a pattern type
# with metavariables, and a structural matcher. Exercises a multi-module whole-
# library compile with real inter-module ordering (ocamldep -sort), .mli
# interfaces, an inter-library dep (:toycaml_intern), and a real fetched-from-
# source opam dependency (`re`, used to validate identifier tokens). Depends on
# `re` alone -- not the vendored `fmt` -- to avoid re's internal `Fmt` unit.
ocaml_library(
    name = "toycaml_lib",
    srcs = [
        "tc_ast.ml",
        "tc_ast.mli",
        "tc_lexer.ml",
        "tc_lexer.mli",
        "tc_matcher.ml",
        "tc_matcher.mli",
        "tc_parse.ml",
        "tc_parse.mli",
        "tc_pattern.ml",
        "tc_pattern.mli",
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
