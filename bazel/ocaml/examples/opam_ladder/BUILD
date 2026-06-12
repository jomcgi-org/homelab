load("@bazel_skylib//rules:build_test.bzl", "build_test")

# The ppxlib dependency ladder, built from the pinned lockfile
# (bazel/ocaml/opam/lock.json): every target here is an opam package compiled
# from source by our rules, either via dune2bazel translation or an
# opam/overrides/ BUILD. Green here means the full bootstrap -- compiler-libs
# introspection, per-version AST preprocessing, in-tree codegen, ocamllex /
# ocamlyacc, cppo, and a linked metaquot ppx driver -- works on RBE.
build_test(
    name = "ladder_builds",
    targets = [
        "@ocaml_sexplib0//:sexplib0",
        "@ocaml_stdlib_shims//:stdlib_shims",
        "@ocaml_ppx_derivers//:ppx_derivers",
        "@ocaml_compiler_libs_pkg//:ocaml_common",
        "@ocaml_compiler_libs_pkg//:ocaml_shadow",
        "@ocaml_cppo//:cppo",
        "@ocaml_ppxlib//:astlib",
        "@ocaml_ppxlib//:ppxlib_ast",
        "@ocaml_ppxlib//:ppxlib",
        "@ocaml_ppxlib//:ppxlib_metaquot",
        "@ocaml_ppx_deriving//:ppx_deriving_runtime",
        "@ocaml_ppx_deriving//:ppx_deriving_api",
        "@ocaml_ppx_deriving//:ppx_deriving_show",
        "@ocaml_menhir//:menhirLib",
        "@ocaml_menhir//:menhirSdk",
        "@ocaml_menhir//:menhir",
        "@ocaml_easy_format//:easy_format",
        "@ocaml_camlp_streams//:camlp_streams",
        "@ocaml_biniou//:biniou",
        "@ocaml_yojson//:yojson",
        "@ocaml_atd//:atd",
        "@ocaml_atd//:atdgen_runtime",
        "@ocaml_atd//:atdgen_emit",
        "@ocaml_atd//:atdgen",
        # Phase 8 wave A: Semgrep direct deps (non-dune overrides).
        "@ocaml_cmdliner//:cmdliner",
        "@ocaml_logs//:logs",
        "@ocaml_logs//:logs_fmt",
        "@ocaml_logs//:logs_cli",
        # Phase 8 wave A: lwt core (translated) + its ocplib-endian deps.
        "@ocaml_ocplib_endian//:ocplib_endian",
        "@ocaml_ocplib_endian//:ocplib_endian_bigstring",
        "@ocaml_lwt//:lwt",
    ],
)
