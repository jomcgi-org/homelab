"""Pinned source for the Semgrep CE tree (Phase 8, wave D).

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
]
