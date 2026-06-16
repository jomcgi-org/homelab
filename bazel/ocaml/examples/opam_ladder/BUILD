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
        # Phase 8 wave B: the ppx half (translated; ppxlib.traverse joined the
        # ppxlib override for ppx_sexp_conv's expander). The e2e deriving test
        # lives in examples/jst_ppx.
        "@ocaml_ppxlib//:ppxlib_traverse",
        "@ocaml_ppxlib_jane//:ppxlib_jane",
        "@ocaml_ppx_compare//:ppx_compare",
        "@ocaml_ppx_compare//:ppx_compare_lib",
        "@ocaml_ppx_sexp_conv//:ppx_sexp_conv",
        "@ocaml_ppx_sexp_conv//:ppx_sexp_conv_lib",
        "@ocaml_ppx_hash//:ppx_hash",
        "@ocaml_ppx_hash//:ppx_hash_lib",
        # Phase 8 wave D: first target out of the pinned Semgrep CE tree.
        "@semgrep_src//:collections",
        # Phase 8 wave A4: the libs/commons closure (see
        # semgrep_src/README.md for the dispatch).
        "@ocaml_fpath//:fpath",
        "@ocaml_astring//:astring",
        "@ocaml_rresult//:rresult",
        "@ocaml_fmt//:fmt",
        "@ocaml_fmt//:fmt_tty",
        "@ocaml_uuidm//:uuidm",
        "@ocaml_hmap//:hmap",
        "@ocaml_ptime//:ptime",
        "@ocaml_mtime//:mtime",
        "@ocaml_mtime//:mtime_clock",
        "@ocaml_bos//:bos",
        "@ocaml_ANSITerminal//:ANSITerminal",
        "@ocaml_hex//:hex",
        "@ocaml_semver//:semver",
        "@ocaml_memtrace//:memtrace",
        "@ocaml_optint//:optint",
        "@ocaml_psq//:psq",
        "@ocaml_lwt_dllist//:lwt_dllist",
        "@ocaml_thread_table//:thread_table",
        "@ocaml_domain_local_await//:domain_local_await",
        "@ocaml_cstruct//:cstruct",
        "@ocaml_eqaf//:eqaf",
        "@ocaml_digestif//:digestif",
        "@ocaml_parsexp//:parsexp",
        "@ocaml_sexplib//:sexplib",
        "@ocaml_timedesc_tzdb//:timedesc_tzdb",
        "@ocaml_timedesc_tzlocal//:timedesc_tzlocal",
        "@ocaml_timedesc//:timedesc",
        "@ocaml_ppx_deriving//:ppx_deriving_eq",
        "@ocaml_ppx_deriving//:ppx_deriving_ord",
        "@ocaml_ppx_deriving_yojson//:ppx_deriving_yojson",
        "@ocaml_ppx_deriving_yojson//:ppx_deriving_yojson_runtime",
        "@ocaml_testo//:testo_diff",
        "@ocaml_testo//:testo_util",
        "@ocaml_testo//:testo",
        # Phase 8 wave A4: the eio half of the closure (commons references
        # Eio + Eio_unix; the io_uring backends stay unbuilt).
        "@ocaml_eio//:eio__core",
        "@ocaml_eio//:eio",
        "@ocaml_eio//:eio_utils",
        "@ocaml_eio//:eio_unix",
        # Phase 8 wave D: the tree-sitter runtime binding chain (the bash
        # grammar itself is exercised by examples/treesitter_bash, no-arm64).
        "@ocaml_tsort//:tsort",
        "@ocaml_tree_sitter_core//:tree_sitter_gen",
        # Phase 8 telemetry closure: the semgrep opentelemetry fork (git
        # pin) and its deps; the exporter clients stay unbuilt (see
        # semgrep_src/README.md). libs/telemetry + libs/parallelism are the
        # first internal dirs compiled with real (non-stub) telemetry.
        "@ocaml_pbrt//:pbrt",
        "@ocaml_thread_local_storage//:thread_local_storage",
        "@ocaml_ambient_context//:ambient_context",
        "@ocaml_ambient_context//:ambient_context_lwt",
        "@ocaml_ptime//:ptime_clock",
        "@ocaml_opentelemetry//:opentelemetry_proto",
        "@ocaml_opentelemetry//:opentelemetry",
        "@ocaml_opentelemetry//:opentelemetry_client",
        "@ocaml_opentelemetry//:opentelemetry_logs",
        "@ocaml_opentelemetry//:opentelemetry_ambient_context_eio",
        "@semgrep_src//:telemetry",
        "@semgrep_src//:parallelism",
        # Phase 8 ast_generic slice: visitors (translated; runtime + the
        # ppx_deriver rewriter), the one new external the generic AST needs.
        "@ocaml_visitors//:VisitorsRuntime",
        "@ocaml_visitors//:ppx_deriving_visitors",
        # Phase 8 semgrep-core closure: base64 (src/sca's one new external;
        # override: the unsafe.ml copy-rule probe pins to unsafe_stable.ml
        # on the 5.3 sysroot).
        "@ocaml_base64//:base64",
        "@ocaml_base64//:base64_rfc2045",
        # Phase 8 yaml slice: the ctypes closure src/core's yaml dep rides
        # on. integers/ctypes use c_srcs (not cc_library), so they build on
        # both arch shards; ctypes_primitives.ml comes from the real
        # configurator probe run on the default pool (the lwt_features
        # mold), identical output on both LP64 little-endian platforms.
        "@ocaml_integers//:integers",
        "@ocaml_ctypes//:ctypes",
        "@ocaml_ctypes//:ctypes_stubs",
    ],
)

# The cc_deps half of the ladder: everything here reaches a vendored C
# library (commons links pcre + pcre2), and Bazel's C++ toolchain in CI
# targets x86_64 only -- the established no-arm64 pattern (the OCaml driver
# itself is per-arch native; only cc_library is restricted).
build_test(
    name = "ladder_builds_cc",
    tags = ["no-arm64"],
    targets = [
        "@ocaml_pcre//:pcre",
        # Phase 8 wave D: libs/commons and libs/glob from the pinned tree
        # (commons' dune overlaid to its measured dependency set; glob brings
        # the internal profiling ppx and a menhir parser).
        "@semgrep_src//:commons",
        "@semgrep_src//:process_limits",
        "@semgrep_src//:profiling",
        "@semgrep_src//:ppx_profiling",
        "@semgrep_src//:glob",
        # Phase 8 path/parsing layer: all reach pcre/pcre2 through commons,
        # so they live in the cc bucket with it.
        "@semgrep_src//:commons2",
        "@semgrep_src//:paths",
        "@semgrep_src//:gitignore",
        "@semgrep_src//:lib_parsing",
        "@semgrep_src//:lib_parsing_tree_sitter",
        # Phase 8 source-to-generic-AST slice: the generic AST and the Go
        # parser chain (all reach commons, hence the cc bucket; the grammar
        # itself is a cc_library besides).
        "@semgrep_src//:ast_generic",
        "@semgrep_src//:parser_go_ast",
        "@semgrep_src//:parser_go_tree_sitter",
        "@semgrep_src//:parser_go_ast_generic",
        # Phase 8 semgrep-core closure slice: the internal commons/telemetry
        # rewriters (the profiling/ppx shape) and src/configuring, the first
        # dir whose atdgen codegen rules are translated (not overridden).
        "@semgrep_src//:ppx_commons",
        "@semgrep_src//:ppx_telemetry",
        "@semgrep_src//:semgrep_configuring",
        # Phase 8 semgrep_core closure frontier: src/core's own dependency
        # set minus src/core itself (gated on yaml/ctypes, see
        # semgrep_src/README.md). All reach commons, hence the cc bucket.
        "@semgrep_src//:spacegrep",
        "@semgrep_src//:parser_javascript_ast",
        "@semgrep_src//:aliengrep",
        "@semgrep_src//:semgrep_interfaces",
        "@semgrep_src//:semgrep_core_rule",
        "@semgrep_src//:semgrep_core_sca",
        "@semgrep_src//:git_wrapper",
        "@semgrep_src//:semgrep_core_target",
        # Phase 8 yaml slice: ocaml-yaml links the vendored libyaml
        # cc_library, hence the cc bucket; src/core (the closure this slice
        # closes) reaches it AND commons' pcre.
        "@ocaml_yaml//:yaml",
        "@semgrep_src//:semgrep_core",
        # Phase 9 wave 1: the cheapest src/parsing consumers. Both reach
        # commons' pcre (fast_json through lib_parsing, typing directly),
        # so they live in the cc bucket; neither adds a lock entry.
        "@semgrep_src//:fast_json",
        "@semgrep_src//:semgrep_typing",
        # Phase 9 wave 2: the languages/yaml parser trio. parser + generic
        # link @ocaml_yaml//:yaml, so this is the second consumer of the yaml
        # cc_library override (src/core was the first); all reach commons' pcre.
        "@semgrep_src//:parser_yaml_ast",
        "@semgrep_src//:parser_yaml_parser",
        "@semgrep_src//:parser_yaml_ast_generic",
    ],
)
