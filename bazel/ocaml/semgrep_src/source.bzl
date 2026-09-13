"""Pinned source for the Semgrep CE tree and native semgrep-core engine.

Pinned by commit for reproducibility, like the compiler fork in
toolchain/source.bzl. Rationale for this pin: the `develop` tip on the pin
date (2026-06-12). The opam floors in semgrep.opam at this commit are what
bazel/ocaml/opam/lock.json mirrors (lwt 5.9.2, uri >= 4.4.0,
ocamlgraph >= 2.2.0, parmap >= 1.2.5, base >= v0.17.3, ppx_hash >= v0.17.0,
ppx_sexp_conv >= v0.17.1); bumping the pin means re-checking those floors.

The clone is shallow and does NOT init submodules: the `languages/`
tree-sitter grammars ride the opam lock as `"opam": false` entries instead
(wave C), pinned to the commits the submodules reference.

SEMGREP_SRC_DIRS is the translated frontier: the dune dirs dune2bazel runs
over at fetch time, growing bottom-up exactly like the opam universe.
SEMGREP_LIBS maps each translated library's dune name to its target so
later dirs can reference earlier ones. Libraries whose public name differs
from their dune (name ...) get TWO keys: upstream uses them
interchangeably -- pps lines name internal rewriters either way
(src/configuring says ppx_profiling, src/core says commons.ppx AND
ppx_telemetry), and src/core's (libraries ...) names semgrep_core_rule /
semgrep_core_target while src/sca and src/target use the semgrep.* public
names. Only dune's in-project resolution knows the internal names, so the
map carries both. OVERLAYS lists tree paths
replaced by overlays/<path> before translation; every overlay documents
what it changes and why (the reject-loudly contract's "source patch"
dispatch).
"""

SEMGREP_GIT_URL = "https://github.com/semgrep/semgrep.git"

# Tip of `develop` on 2026-06-12.
SEMGREP_COMMIT = "872766d4b93fc9d4b0e414c0afd9ed4e99171c6c"

SEMGREP_SRC_DIRS = [
    "libs/collections",
    "libs/telemetry",
    "libs/telemetry/ppx",
    "libs/parallelism",
    "libs/commons",
    "libs/commons/ppx",
    "libs/process_limits",
    "libs/profiling",
    "libs/profiling/ppx",
    "libs/glob",
    "libs/commons2",
    "libs/paths",
    "libs/gitignore",
    "libs/lib_parsing",
    "libs/lib_parsing_tree_sitter",
    "src/ast_generic",
    "src/configuring",
    "languages/go/ast",
    "languages/go/tree-sitter",
    "languages/go/generic",
    # The semgrep_core closure frontier (src/core's own deps; src/core
    # itself waits on yaml/ctypes, see README). spacegrep's root dune is
    # (dirs ...)-only, so src/lib is listed directly; bin/test stay out.
    "src/spacegrep/src/lib",
    "languages/javascript/ast",
    "src/aliengrep",
    # At this pin a vendored tree (not a submodule); the atd-generated
    # _t/_j sources are checked in, and its complete (modules ...) list
    # validates-and-drops in the translator.
    "cli/src/semgrep/semgrep_interfaces",
    "src/rule",
    "src/sca",
    # src/target's Origin/Target reference Git_wrapper, the moment the old
    # lib_parsing dispatch deferred to. Overlays keep ocaml-git out (see
    # overlays/libs/git_wrapper/ and README).
    "libs/git_wrapper",
    "src/target",
    # The semgrep_core closure closes: yaml (the one name that gated it)
    # rides the lock via the two-stage ctypes stubgen override. The (env ...)
    # block is inert; the tests/ subdir has its own dune and stays out via
    # the non-recursive glob.
    "src/core",
    # Phase 9 wave 1: the cheapest src/parsing consumers, both pure (no new
    # lock entries). fast_json names lib_parsing/paths/yojson/ast_generic;
    # typing names commons/lib_parsing/parallelism/semgrep_core with a
    # ppx_deriving.show + ppx_profiling pps line. Both already resolved.
    "libs/fast_json",
    "src/typing",
    # Phase 9 wave 2: the languages/yaml parser trio, the first consumers of
    # the yaml lock outside src/core (parser + generic name `yaml` directly).
    # All three are clean translates (commons/lib_parsing/ast_generic internal,
    # yaml locked, ppx_deriving.show pps locked); no menhir/lex/stubs. ast
    # first, then parser (names ast), then generic (names both).
    "languages/yaml/ast",
    "languages/yaml/parser",
    "languages/yaml/generic",
    # Phase 9 wave 3: src/il + src/analyzing. src/analyzing names semgrep.il,
    # so src/il lands first; src/il names `ograph` (libs/ograph, the
    # deprecated object-graph lib: commons + collections, pure, no pps), so
    # that internal dir lands ahead of both. ograph -> il -> analyzing, all
    # reaching commons' pcre. src/il's pps adds ppx_deriving.eq/.ord and
    # visitors.ppx to the show line; all locked (ast_generic already drives
    # visitors). No new lock entries.
    "libs/ograph",
    "src/il",
    "src/analyzing",
    # Phase 9 wave 4: src/prefiltering. Names ast_generic/semgrep.core/
    # semgrep.target (all translated) with a show/.eq/.ord/ppx_hash/
    # ppx_profiling/ppx_sexp_conv pps line (all locked). Two atdgen (rule)
    # pairs over Semgrep_prefilter.atd (-j -j-strict-fields / -t), the exact
    # genrule shape src/configuring already translates. atdgen-runtime (the
    # generated _j's dep) is locked and reaches it transitively through
    # semgrep.core -> semgrep_interfaces, as upstream resolves it. No new
    # externals.
    "src/prefiltering",
    # Phase 9 wave 5: src/targeting. The first NEW external since yaml:
    # ppx_blob (locked; a clean ppx_rewriter translate, no override). Names
    # commons/fpath/glob/gitignore/git_wrapper/semgrep.target/semgrep.core
    # (all translated/locked) with a show/yojson/.eq/ppx_hash/ppx_profiling/
    # telemetry.ppx/commons.ppx/ppx_blob pps line. Carries the first
    # (preprocessor_deps (file default.semgrepignore)) stanza, handled by the
    # new translator preprocess_data feature: default.semgrepignore rides the
    # shallow clone as a regular file and is staged into the ppx work dir so
    # ppx_blob's [%blob "default.semgrepignore"] resolves at preprocess time.
    "src/targeting",
    # Phase 9 wave 6: src/naming. Clean mechanical translate; names
    # commons/ast_generic/semgrep.core/semgrep.typing/parser_javascript.ast
    # (all translated) with a (pps ppx_profiling ppx_deriving.show) line, all
    # locked. Reaches commons' pcre, so ladder_builds_cc. No new externals.
    "src/naming",
    # Phase 9 wave 7: the jsonnet source-to-generic chain plus libs/ojsonnet,
    # the consumer that motivates it. The one new external is the grammar:
    # tree-sitter-lang.jsonnet stamps into the lock from the
    # languages/jsonnet/tree-sitter submodule commit (the tree-sitter-go
    # pattern, but the jsonnet grammar HAS an external scanner). ast lands
    # first (commons + lib_parsing), then tree-sitter (names parser_jsonnet.ast
    # + the grammar), then generic (names parser_jsonnet.ast + ast_generic),
    # then ojsonnet (names parser_jsonnet.tree_sitter). All pps lines are
    # ppx_deriving.show (+ ojsonnet's ppx_profiling/ppx_deriving.ord/commons.ppx),
    # all locked; `unix` is stdlib (the lib_map resolves it). The grammar chain
    # rides ladder_builds_cc like parser_go_tree_sitter on both CI architectures.
    "languages/jsonnet/ast",
    "languages/jsonnet/tree-sitter",
    "languages/jsonnet/generic",
    "libs/ojsonnet",
    # Spacegrep remains a direct generic-mode engine target. Its library
    # closure is already complete above: the executable adds cmdliner
    # (pinned in the opam lock) and unix (the compiler stdlib) and reaches the
    # vendored PCRE/PCRE2 native libraries through commons. The overlay rewrites
    # upstream's plural one-program stanza to the supported singular form and
    # selects flags.sh's non-Alpine Linux dynamic policy (no extra flags). Bazel
    # carries the required native archives explicitly through cc_deps.
    "src/spacegrep/src/bin",
    # Complete issue #3922 with the upstream all-language parsing frontier,
    # Semgrep matching/taint/fix engine, core scan orchestrator, and a native
    # semgrep-core entry point. The language order is dependency-topological;
    # every tree-sitter binding is checksum-locked to the corresponding
    # submodule commit from this same Semgrep pin.
    "languages/bash/ast",
    "languages/cairo/generic",
    "languages/circom/generic",
    "languages/cpp/ast",
    "languages/csharp/generic",
    "languages/dart/generic",
    "languages/fga/generic",
    "languages/go/menhir",
    "languages/hack/generic",
    "languages/html/generic",
    "languages/java/ast",
    "languages/javascript/generic",
    "languages/javascript/menhir",
    "languages/json/ast",
    "languages/julia/generic",
    "languages/kotlin/generic",
    "languages/lisp/tree-sitter",
    "languages/lua/generic",
    "languages/move_on_aptos/generic",
    "languages/move_on_sui/generic",
    "languages/ocaml/ast",
    "languages/php/ast",
    "languages/promql/generic",
    "languages/protobuf/generic",
    "languages/python/ast",
    "languages/ql/ast",
    "languages/r/generic",
    "languages/regexp",
    "languages/ruby/ast",
    "languages/rust/generic",
    "languages/scala/ast",
    "languages/solidity/generic",
    "languages/swift/generic",
    "languages/terraform/ast",
    "languages/typescript/tree-sitter",
    "languages/bash/generic",
    "languages/cpp/generic",
    "languages/cpp/menhir",
    "languages/dockerfile/ast",
    "languages/java/generic",
    "languages/java/tree-sitter",
    "languages/json/generic",
    "languages/json/menhir",
    "languages/ocaml/generic",
    "languages/ocaml/menhir",
    "languages/ocaml/tree-sitter",
    "languages/php/generic",
    "languages/php/menhir",
    "languages/php/tree-sitter",
    "languages/python/generic",
    "languages/python/menhir",
    "languages/python/tree-sitter",
    "languages/ql/generic",
    "languages/ql/tree-sitter",
    "languages/ruby/generic",
    "languages/ruby/tree-sitter",
    "languages/scala/generic",
    "languages/scala/recursive_descent",
    "languages/terraform/generic",
    "languages/terraform/tree-sitter",
    "languages/bash/tree-sitter",
    "languages/cpp/tree-sitter",
    "languages/dockerfile/generic",
    "languages/dockerfile/tree-sitter",
    "src/parsing",
    "src/printing",
    "src/reporting",
    "src/matching",
    "src/tainting",
    "src/fixing",
    "src/engine",
    "src/core_scan",
    "src/main_core",
]

SEMGREP_LIBS = {
    "collections": ":collections",
    "telemetry": ":telemetry",
    "telemetry.ppx": ":ppx_telemetry",
    "ppx_telemetry": ":ppx_telemetry",
    "parallelism": ":parallelism",
    "commons": ":commons",
    "commons.ppx": ":ppx_commons",
    "ppx_commons": ":ppx_commons",
    "process_limits": ":process_limits",
    "profiling": ":profiling",
    "profiling.ppx": ":ppx_profiling",
    "ppx_profiling": ":ppx_profiling",
    "glob": ":glob",
    "commons2": ":commons2",
    "paths": ":paths",
    "gitignore": ":gitignore",
    "lib_parsing": ":lib_parsing",
    "lib_parsing_tree_sitter": ":lib_parsing_tree_sitter",
    "ast_generic": ":ast_generic",
    "semgrep.configuring": ":semgrep_configuring",
    "parser_go.ast": ":parser_go_ast",
    "parser_go.tree_sitter": ":parser_go_tree_sitter",
    "parser_go.ast_generic": ":parser_go_ast_generic",
    "spacegrep": ":spacegrep",
    "parser_javascript.ast": ":parser_javascript_ast",
    "parser_javascript_ast": ":parser_javascript_ast",
    "aliengrep": ":aliengrep",
    "semgrep.interfaces": ":semgrep_interfaces",
    "semgrep_interfaces": ":semgrep_interfaces",
    "git_wrapper": ":git_wrapper",
    # Non-rewriter libraries get both names too: src/core's stanza names
    # semgrep_core_rule/semgrep_core_target by their dune (name ...) while
    # src/sca and src/target use the semgrep.* public names.
    "semgrep.rule": ":semgrep_core_rule",
    "semgrep_core_rule": ":semgrep_core_rule",
    "semgrep.sca": ":semgrep_core_sca",
    "semgrep_core_sca": ":semgrep_core_sca",
    "semgrep.target": ":semgrep_core_target",
    "semgrep_core_target": ":semgrep_core_target",
    "semgrep.core": ":semgrep_core",
    "semgrep_core": ":semgrep_core",
    # Phase 9 wave 1.
    "fast_json": ":fast_json",
    "semgrep.typing": ":semgrep_typing",
    "semgrep_typing": ":semgrep_typing",
    # Phase 9 wave 2.
    "parser_yaml.ast": ":parser_yaml_ast",
    "parser_yaml_ast": ":parser_yaml_ast",
    "parser_yaml.parser": ":parser_yaml_parser",
    "parser_yaml_parser": ":parser_yaml_parser",
    "parser_yaml.ast_generic": ":parser_yaml_ast_generic",
    "parser_yaml_ast_generic": ":parser_yaml_ast_generic",
    # Phase 9 wave 3.
    "ograph": ":ograph",
    "semgrep.il": ":semgrep_core_il",
    "semgrep_core_il": ":semgrep_core_il",
    "pfff-lang_GENERIC-analyze": ":pfff_lang_GENERIC_analyze",
    "pfff_lang_GENERIC_analyze": ":pfff_lang_GENERIC_analyze",
    # Phase 9 wave 4.
    "semgrep.prefiltering": ":prefiltering",
    "prefiltering": ":prefiltering",
    # Phase 9 wave 5.
    "semgrep.targeting": ":semgrep_targeting",
    "semgrep_targeting": ":semgrep_targeting",
    # Phase 9 wave 6.
    "pfff-lang_GENERIC-naming": ":pfff_lang_GENERIC_naming",
    "pfff_lang_GENERIC_naming": ":pfff_lang_GENERIC_naming",
    # Phase 9 wave 7.
    "parser_jsonnet.ast": ":parser_jsonnet_ast",
    "parser_jsonnet_ast": ":parser_jsonnet_ast",
    "parser_jsonnet.tree_sitter": ":parser_jsonnet_tree_sitter",
    "parser_jsonnet_tree_sitter": ":parser_jsonnet_tree_sitter",
    "parser_jsonnet.ast_generic": ":parser_jsonnet_ast_generic",
    "parser_jsonnet_ast_generic": ":parser_jsonnet_ast_generic",
    # ojsonnet's dune has no (name ...), so dune name == public name.
    "ojsonnet": ":ojsonnet",
    # Complete CE parser and engine closure (issue #3922). Public and internal
    # dune names both resolve because upstream uses both forms.
    "parser_bash.ast": ":parser_bash_ast",
    "parser_bash_ast": ":parser_bash_ast",
    "parser_cairo.ast_generic": ":parser_cairo_ast_generic",
    "parser_cairo_ast_generic": ":parser_cairo_ast_generic",
    "parser_circom.ast_generic": ":parser_circom_ast_generic",
    "parser_circom_ast_generic": ":parser_circom_ast_generic",
    "parser_cpp.ast": ":parser_cpp_ast",
    "parser_cpp_ast": ":parser_cpp_ast",
    "parser_csharp.ast_generic": ":parser_csharp_ast_generic",
    "parser_csharp_ast_generic": ":parser_csharp_ast_generic",
    "parser_dart.ast_generic": ":parser_dart_ast_generic",
    "parser_dart_ast_generic": ":parser_dart_ast_generic",
    "parser_fga.ast_generic": ":parser_fga_ast_generic",
    "parser_fga_ast_generic": ":parser_fga_ast_generic",
    "parser_go.menhir": ":pfff_lang_go",
    "pfff_lang_go": ":pfff_lang_go",
    "parser_hack.ast_generic": ":parser_hack_ast_generic",
    "parser_hack_ast_generic": ":parser_hack_ast_generic",
    "parser_html.ast_generic": ":parser_html_ast_generic",
    "parser_html_ast_generic": ":parser_html_ast_generic",
    "parser_java.ast": ":parser_java_ast",
    "parser_java_ast": ":parser_java_ast",
    "parser_javascript.ast_generic": ":parser_javascript_ast_generic",
    "parser_javascript_ast_generic": ":parser_javascript_ast_generic",
    "parser_javascript.menhir": ":parser_javascript_menhir",
    "parser_javascript_menhir": ":parser_javascript_menhir",
    "parser_json.ast": ":parser_json_ast",
    "parser_json_ast": ":parser_json_ast",
    "parser_julia.ast_generic": ":parser_julia_ast_generic",
    "parser_julia_ast_generic": ":parser_julia_ast_generic",
    "parser_kotlin.ast_generic": ":parser_kotlin_ast_generic",
    "parser_kotlin_ast_generic": ":parser_kotlin_ast_generic",
    "parser_lisp.tree_sitter": ":parser_lisp_tree_sitter",
    "parser_lisp_tree_sitter": ":parser_lisp_tree_sitter",
    "parser_lua.ast_generic": ":parser_lua_ast_generic",
    "parser_lua_ast_generic": ":parser_lua_ast_generic",
    "parser_move_on_aptos.ast_generic": ":parser_move_on_aptos_ast_generic",
    "parser_move_on_aptos_ast_generic": ":parser_move_on_aptos_ast_generic",
    "parser_move_on_sui.ast_generic": ":parser_move_on_sui_ast_generic",
    "parser_move_on_sui_ast_generic": ":parser_move_on_sui_ast_generic",
    "parser_ocaml.ast": ":parser_ocaml_ast",
    "parser_ocaml_ast": ":parser_ocaml_ast",
    "parser_php.ast": ":parser_php_ast",
    "parser_php_ast": ":parser_php_ast",
    "parser_promql.ast_generic": ":parser_promql_ast_generic",
    "parser_promql_ast_generic": ":parser_promql_ast_generic",
    "parser_protobuf.ast_generic": ":parser_protobuf_ast_generic",
    "parser_protobuf_ast_generic": ":parser_protobuf_ast_generic",
    "parser_python.ast": ":parser_python_ast",
    "parser_python_ast": ":parser_python_ast",
    "parser_ql.ast": ":parser_ql_ast",
    "parser_ql_ast": ":parser_ql_ast",
    "parser_r.ast_generic": ":parser_r_ast_generic",
    "parser_r_ast_generic": ":parser_r_ast_generic",
    "parser_regexp": ":parser_regexp",
    "parser_ruby.ast": ":parser_ruby_ast",
    "parser_ruby_ast": ":parser_ruby_ast",
    "parser_rust.ast_generic": ":parser_rust_ast_generic",
    "parser_rust_ast_generic": ":parser_rust_ast_generic",
    "parser_scala.ast": ":parser_scala_ast",
    "parser_scala_ast": ":parser_scala_ast",
    "parser_solidity.ast_generic": ":parser_solidity_ast_generic",
    "parser_solidity_ast_generic": ":parser_solidity_ast_generic",
    "parser_swift.ast_generic": ":parser_swift_ast_generic",
    "parser_swift_ast_generic": ":parser_swift_ast_generic",
    "parser_terraform.ast": ":parser_terraform_ast",
    "parser_terraform_ast": ":parser_terraform_ast",
    "parser_typescript.tree_sitter": ":parser_typescript_tree_sitter",
    "parser_typescript_tree_sitter": ":parser_typescript_tree_sitter",
    "parser_bash.ast_generic": ":parser_bash_ast_generic",
    "parser_bash_ast_generic": ":parser_bash_ast_generic",
    "parser_cpp.ast_generic": ":parser_cpp_ast_generic",
    "parser_cpp_ast_generic": ":parser_cpp_ast_generic",
    "parser_cpp.menhir": ":parser_cpp_menhir",
    "parser_cpp_menhir": ":parser_cpp_menhir",
    "parser_dockerfile.ast": ":parser_dockerfile_ast",
    "parser_dockerfile_ast": ":parser_dockerfile_ast",
    "parser_java.ast_generic": ":parser_java_ast_generic",
    "parser_java_ast_generic": ":parser_java_ast_generic",
    "parser_java.tree_sitter": ":parser_java_tree_sitter",
    "parser_java_tree_sitter": ":parser_java_tree_sitter",
    "parser_json.ast_generic": ":parser_json_ast_generic",
    "parser_json_ast_generic": ":parser_json_ast_generic",
    "parser_json.menhir": ":parser_json_menhir",
    "parser_json_menhir": ":parser_json_menhir",
    "parser_ocaml.ast_generic": ":parser_ocaml_ast_generic",
    "parser_ocaml_ast_generic": ":parser_ocaml_ast_generic",
    "parser_ocaml.menhir": ":parser_ocaml_menhir",
    "parser_ocaml_menhir": ":parser_ocaml_menhir",
    "parser_ocaml.tree_sitter": ":parser_ocaml_tree_sitter",
    "parser_ocaml_tree_sitter": ":parser_ocaml_tree_sitter",
    "parser_php.ast_generic": ":parser_php_ast_generic",
    "parser_php_ast_generic": ":parser_php_ast_generic",
    "parser_php.menhir": ":parser_php_menhir",
    "parser_php_menhir": ":parser_php_menhir",
    "parser_php.tree_sitter": ":parser_php_tree_sitter",
    "parser_php_tree_sitter": ":parser_php_tree_sitter",
    "parser_python.ast_generic": ":parser_python_ast_generic",
    "parser_python_ast_generic": ":parser_python_ast_generic",
    "parser_python.menhir": ":parser_python_menhir",
    "parser_python_menhir": ":parser_python_menhir",
    "parser_python.tree_sitter": ":parser_python_tree_sitter",
    "parser_python_tree_sitter": ":parser_python_tree_sitter",
    "parser_ql.ast_generic": ":parser_ql_ast_generic",
    "parser_ql_ast_generic": ":parser_ql_ast_generic",
    "parser_ql.tree_sitter": ":parser_ql_tree_sitter",
    "parser_ql_tree_sitter": ":parser_ql_tree_sitter",
    "parser_ruby.ast_generic": ":parser_ruby_ast_generic",
    "parser_ruby_ast_generic": ":parser_ruby_ast_generic",
    "parser_ruby.tree_sitter": ":parser_ruby_tree_sitter",
    "parser_ruby_tree_sitter": ":parser_ruby_tree_sitter",
    "parser_scala.ast_generic": ":parser_scala_ast_generic",
    "parser_scala_ast_generic": ":parser_scala_ast_generic",
    "parser_scala.recursive_descent": ":parser_scala_recursive_descent",
    "parser_scala_recursive_descent": ":parser_scala_recursive_descent",
    "parser_terraform.ast_generic": ":parser_terraform_ast_generic",
    "parser_terraform_ast_generic": ":parser_terraform_ast_generic",
    "parser_terraform.tree_sitter": ":parser_terraform_tree_sitter",
    "parser_terraform_tree_sitter": ":parser_terraform_tree_sitter",
    "parser_bash.tree_sitter": ":parser_bash_tree_sitter",
    "parser_bash_tree_sitter": ":parser_bash_tree_sitter",
    "parser_cpp.tree_sitter": ":parser_cpp_tree_sitter",
    "parser_cpp_tree_sitter": ":parser_cpp_tree_sitter",
    "parser_dockerfile.ast_generic": ":parser_dockerfile_ast_generic",
    "parser_dockerfile_ast_generic": ":parser_dockerfile_ast_generic",
    "parser_dockerfile.tree_sitter": ":parser_dockerfile_tree_sitter",
    "parser_dockerfile_tree_sitter": ":parser_dockerfile_tree_sitter",
    "semgrep.parsing": ":semgrep_parsing",
    "semgrep_parsing": ":semgrep_parsing",
    "semgrep.printing": ":semgrep_printing",
    "semgrep_printing": ":semgrep_printing",
    "semgrep.reporting": ":semgrep_reporting",
    "semgrep_reporting": ":semgrep_reporting",
    "semgrep.matching": ":semgrep_matching",
    "semgrep_matching": ":semgrep_matching",
    "semgrep.tainting": ":semgrep_tainting",
    "semgrep_tainting": ":semgrep_tainting",
    "semgrep.fixing": ":semgrep_fixing",
    "semgrep_fixing": ":semgrep_fixing",
    "semgrep.engine": ":semgrep_engine",
    "semgrep_engine": ":semgrep_engine",
    "semgrep.core_scan": ":semgrep_core_scan",
    "semgrep_core_scan": ":semgrep_core_scan",
}

OVERLAYS = [
    "libs/collections/dune",
    "libs/telemetry/dune",
    "libs/telemetry/Telemetry.ml",
    "libs/parallelism/dune",
    "libs/commons/dune",
    "libs/commons/Ord.ml",
    "libs/lib_parsing/dune",
    "libs/lib_parsing/Pos.ml",
    "src/aliengrep/dune",
    "src/sca/Dependency.ml",
    "src/sca/Dependency.mli",
    "libs/git_wrapper/dune",
    "libs/git_wrapper/Git_wrapper.ml",
    "libs/git_wrapper/Git_wrapper.mli",
    "src/target/dune",
    "src/spacegrep/src/bin/dune",
    "languages/ocaml/menhir/dune",
    "src/engine/dune",
    "src/fixing/dune",
    "src/main_core/dune",
    "src/main_core/Semgrep_core_main.ml",
]

# Small source patches strip inline tests whose ppx is intentionally outside
# the production engine closure. The corresponding dune overlays below remove
# (inline_tests) and ppx_inline_test, so leaving this syntax would fail loudly.
PATCHES = [
    "src/engine/Match_taint_spec.ml.patch",
    "src/fixing/Autofix.ml.patch",
]
