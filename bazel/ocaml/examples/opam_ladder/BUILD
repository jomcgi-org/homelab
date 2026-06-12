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
        # Phase 8 wave A: lwt.unix (hybrid repo: translated core + hand-written
        # fragment) and the dune-configurator chain its discover probe needs.
        "@ocaml_csexp//:csexp",
        "@ocaml_dune_configurator//:configurator",
        "@ocaml_lwt//:lwt_unix",
        # Phase 8 wave A: the uri chain (translated; bigstringaf overridden for
        # its C stub) and ocamlgraph (translated: include_subdirs + lex/yacc).
        "@ocaml_stringext//:stringext",
        "@ocaml_bigstringaf//:bigstringaf",
        "@ocaml_angstrom//:angstrom",
        "@ocaml_uri//:uri",
        "@ocaml_ocamlgraph//:graph",
        # Phase 8 wave A: parmap (override: configurator probe pinned for
        # linux glibc, C stubs via c_srcs/c_headers).
        "@ocaml_parmap//:parmap",
        # Phase 8 wave B: the Jane Street shallow ladder, step 1.
        "@ocaml_ocaml_intrinsics_kernel//:ocaml_intrinsics_kernel",
        "@ocaml_base//:base_internalhash_types",
        "@ocaml_base//:shadow_stdlib",
        "@ocaml_base//:base",
    ],
)
