load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library", "ocaml_ppx", "ocaml_test")

_OPTIMIZED = ["-O3"]

# tOyCaml -- a deliberately tiny "grep for code" that mirrors the *shape* of how
# a large OCaml analysis engine uses the ecosystem, so the ruleset can be grown
# against a representative target rather than a generic hello-world. See README.md
# and bazel/ARCHITECTURE.md (OCaml section, Decision history: tooling/005).
#
# The BUILD files here are hand-written today; they are the first acceptance
# corpus for the OCaml BUILD generator decided in
# bazel/ARCHITECTURE.md (Decision history: tooling/007) -- the
# generator must reproduce them from the sources alone.
#
# Module files remain tc_-prefixed deliberately. tOyCaml is an unwrapped
# first-party library, so the prefix gives its modules stable, collision-free
# names when linked beside the wider opam universe.

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
    ocamlopt_flags = _OPTIMIZED,
    require_flambda = True,
    visibility = ["//visibility:public"],
)

# A real atdgen build action produces the wire AST types and JSON codec. The
# companion C stub validates the same bytes with the pinned tree-sitter JSON
# grammar before atdgen decodes them, making a fetched grammar part of the
# tOyCaml parse path rather than an isolated example.
genrule(
    name = "tc_wire_gen",
    srcs = ["tc_wire.atd"],
    outs = [
        "tc_wire_j.ml",
        "tc_wire_j.mli",
        "tc_wire_t.ml",
        "tc_wire_t.mli",
    ],
    cmd = "W=$$(mktemp -d) && AG=$$(realpath $(location @ocaml_atd//:atdgen)) && " +
          "cp $(location tc_wire.atd) $$W/tc_wire.atd && " +
          "(cd $$W && $$AG -t tc_wire.atd && $$AG -j -j-std tc_wire.atd) && " +
          "cp $$W/tc_wire_t.ml $(location tc_wire_t.ml) && " +
          "cp $$W/tc_wire_t.mli $(location tc_wire_t.mli) && " +
          "cp $$W/tc_wire_j.ml $(location tc_wire_j.ml) && " +
          "cp $$W/tc_wire_j.mli $(location tc_wire_j.mli)",
    tools = ["@ocaml_atd//:atdgen"],
)

ocaml_library(
    name = "toycaml_wire",
    srcs = [
        "tc_wire_codec.ml",
        "tc_wire_codec.mli",
        "tc_wire_j.ml",
        "tc_wire_j.mli",
        "tc_wire_t.ml",
        "tc_wire_t.mli",
    ],
    c_srcs = ["tc_wire_stubs.c"],
    cc_deps = [
        "@ocaml_tree_sitter_c//:tree_sitter",
        "@ocaml_tree_sitter_json//:tree_sitter_json",
    ],
    ocamlopt_flags = _OPTIMIZED,
    require_flambda = True,
    visibility = ["//visibility:public"],
    deps = [
        "@ocaml_atd//:atdgen_runtime",
        "@ocaml_biniou//:biniou",
        "@ocaml_yojson//:yojson",
    ],
)

# Compose visitors with another production-style deriver. Tc_ast uses the
# generated visitor to count nodes and ppx_deriving.show for diagnostic output.
ocaml_ppx(
    name = "toycaml_ppx",
    ocamlopt_flags = _OPTIMIZED,
    require_flambda = True,
    deps = [
        "@ocaml_ppx_deriving//:ppx_deriving_show",
        "@ocaml_visitors//:ppx_deriving_visitors",
    ],
)

# The matching engine: a generic AST, a hand-written lexer/parser, a pattern type
# with metavariables, and a structural matcher. Exercises a multi-module whole-
# library compile with real inter-module ordering (ocamldep -sort), .mli
# interfaces, inter-library dependencies, and a real vendored C dependency.
# Identifier validation uses the pcre2-ocaml override, whose C stubs bind the
# pinned, non-dune PCRE2 source archive through cc_deps.
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
    ocamlopt_flags = _OPTIMIZED,
    preprocess = ":toycaml_ppx",
    preprocess_runtime_deps = [
        "@ocaml_ppx_deriving//:ppx_deriving_runtime",
        "@ocaml_visitors//:VisitorsRuntime",
    ],
    require_flambda = True,
    visibility = ["//visibility:public"],
    deps = [
        ":toycaml_intern",
        ":toycaml_wire",
        "@ocaml_pcre2//:pcre2",
    ],
)

# The osemgrep-shaped CLI entry point.
ocaml_binary(
    name = "toycaml",
    srcs = ["main.ml"],
    ocamlopt_flags = _OPTIMIZED,
    require_flambda = True,
    static_link = True,
    visibility = ["//visibility:public"],
    deps = [
        ":toycaml_lib",
        "@ocaml_cmdliner//:cmdliner",
    ],
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
    ocamlopt_flags = _OPTIMIZED,
    require_flambda = True,
    deps = [":toycaml_lib"],
)

# Focused runtime acceptance for atdgen, the fetched grammar, visitors, and
# the vendored/non-dune PCRE2 path. The flambda requirement is checked by the
# compile driver before this test is built, and every source compiles at -O3.
ocaml_test(
    name = "toycaml_capabilities_test",
    srcs = ["capabilities_test.ml"],
    ocamlopt_flags = _OPTIMIZED,
    require_flambda = True,
    deps = [
        ":toycaml_lib",
        "@ocaml_pcre2//:pcre2",
    ],
)

# The binary is linked with -static. This test both runs it and rejects an ELF
# PT_INTERP segment, so a silently dynamic final link cannot pass acceptance.
sh_test(
    name = "toycaml_static_link_test",
    srcs = ["static_link_test.sh"],
    args = ["$(rootpath :toycaml)"],
    data = [":toycaml"],
)
