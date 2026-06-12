# @semgrep_src: the pinned Semgrep CE tree (Phase 8, wave D)

`repositories.bzl` clones the commit pinned in `source.bzl`, applies the
`overlays/` files, and runs `opam/dune2bazel.py` over the dune dirs in
`SEMGREP_SRC_DIRS`, resolving `(libraries ...)` against the opam lock plus
the internal libraries translated so far. The translated frontier grows
bottom-up, exactly like the opam universe; anything the translator does not
model rejects loudly at fetch time.

## Translated today

| dir                            | target                     | notes                                                                                                                                                                                                  |
| ------------------------------ | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `libs/collections`             | `:collections`             | dune overlay drops inert `(inline_tests)`/pps                                                                                                                                                          |
| `libs/telemetry`               | `:telemetry`               | dune + `Telemetry.ml` overlays swap the exporter clients for a loud runtime failure (dispatch below)                                                                                                   |
| `libs/parallelism`             | `:parallelism`             | dune overlay drops unreferenced eio_main/eio.mock                                                                                                                                                      |
| `libs/commons`                 | `:commons`                 | dune overlay = measured dep set; `Ord.ml` overlay strips the let%test blocks                                                                                                                           |
| `libs/process_limits`          | `:process_limits`          | translated as-is                                                                                                                                                                                       |
| `libs/profiling`               | `:profiling`               | translated as-is                                                                                                                                                                                       |
| `libs/profiling/ppx`           | `:ppx_profiling`           | the first internal ppx rewriter (kind ppx_deriver)                                                                                                                                                     |
| `libs/glob`                    | `:glob`                    | translated as-is (menhir Parser + ocamllex Lexer)                                                                                                                                                      |
| `libs/commons2`                | `:commons2`                | translated as-is                                                                                                                                                                                       |
| `libs/paths`                   | `:paths`                   | translated as-is (profiling.ppx joins a generated driver)                                                                                                                                              |
| `libs/gitignore`               | `:gitignore`               | translated as-is                                                                                                                                                                                       |
| `libs/lib_parsing`             | `:lib_parsing`             | dune overlay drops inline_tests + unreferenced git_wrapper; `Pos.ml` overlay strips the let%test blocks                                                                                                |
| `libs/lib_parsing_tree_sitter` | `:lib_parsing_tree_sitter` | translated as-is (tree-sitter.run from the locked ocaml-tree-sitter-core); its lightest consumer's grammar, tree-sitter-go, is stamped from the languages/go submodule commit (examples/treesitter_go) |
| `libs/commons/ppx`             | `:ppx_commons`             | translated as-is (commons.ppx, kind ppx_deriver, the profiling/ppx shape)                                                                                                                              |
| `libs/telemetry/ppx`           | `:ppx_telemetry`           | translated as-is (telemetry.ppx; its ppx_tests subdir has its own dune, excluded by the non-recursive glob)                                                                                            |
| `src/ast_generic`              | `:ast_generic`             | translated as-is; the first `src/` entry (everything above is `libs/`). visitors joins the rewriter set (dispatch below)                                                                               |
| `src/configuring`              | `:semgrep_configuring`     | translated as-is; the first dir whose atdgen `(rule ...)` pair is translated (genrules over the locked atdgen) instead of hand-written in an override                                                  |
| `languages/go/ast`             | `:parser_go_ast`           | translated as-is (parser_go.ast)                                                                                                                                                                       |
| `languages/go/tree-sitter`     | `:parser_go_tree_sitter`   | translated as-is (parser_go.tree_sitter); CST -> ast_go glue over the stamped grammar                                                                                                                  |
| `languages/go/generic`         | `:parser_go_ast_generic`   | translated as-is (parser_go.ast_generic); ast_go -> AST_generic (examples/go_generic parses real Go source through the CST into the generic AST)                                                       |
| `src/spacegrep/src/lib`        | `:spacegrep`               | translated as-is (wrapped; ocamllex Lexer). The spacegrep root dune is `(dirs ...)`-only, so src/lib is listed directly; bin/test stay out                                                             |
| `languages/javascript/ast`     | `:parser_javascript_ast`   | translated as-is (parser_javascript.ast; the Ast_js.default_entity wart Rule.ml references)                                                                                                            |
| `src/aliengrep`                | `:aliengrep`               | dune overlay drops unreferenced alcotest (the git_wrapper pattern; ocamldep confirms zero Alcotest references)                                                                                         |
| `cli/src/semgrep/...`          | `:semgrep_interfaces`      | translated as-is once the translator validates-and-drops its complete `(modules ...)` list; at this pin a vendored tree (not a submodule) with the atd-generated `_t`/`_j` sources checked in          |
| `src/rule`                     | `:semgrep_core_rule`       | translated as-is (semgrep.rule); its rule_schema_v2.atd has no `(rule)` stanza at this pin and no source references Rule_schema_v2_t, so the file is inert to the glob                                 |
| `src/sca`                      | `:semgrep_core_sca`        | Dependency.ml{,i} overlays strip the unreferenced Alcotest.testable value (kind_testable), keeping alcotest out of the lock                                                                            |
| `libs/git_wrapper`             | `:git_wrapper`             | dune overlay drops ocaml-git; Git_wrapper.ml{,i} overlays swap the functor types for concrete digestif-backed equivalents and fail loudly in the object-store walkers (dispatch below)                 |
| `src/target`                   | `:semgrep_core_target`     | dune overlay adds git_wrapper (Origin/Target reference it; upstream reached it transitively through lib_parsing, whose overlay measured it out); the `(env ...)` block is inert                        |
| `src/core`                     | `:semgrep_core`            | translated as-is; the semgrep_core closure's keystone. yaml was the only name that gated it (dispatch below); the `(env ...)` -w 30 block is inert (warnings are not errors here); tests/ has its own dune and stays out via the non-recursive glob                                              |

## libs/commons rejection dispatch

Running dune2bazel over `libs/commons` at the pinned commit rejects on
`(inline_tests)` first; the full dune stanza decomposes into the following
named decisions (the wave A4 shopping list). Legend: **lock** = add the opam
package to lock.json (translated unless noted), **override+** = extend an
existing override, **overlay** = source patch via `overlays/`, **internal** =
another `SEMGREP_SRC_DIRS` entry.

| piece                                                                                 | dispatch                                                                                                                                                                              |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `(inline_tests)`                                                                      | overlay: drop the field AND strip the 6 `let%test` blocks in `Ord.ml` (without ppx_inline_test the syntax does not parse; decided in the plan, do not re-litigate)                    |
| `ppx_inline_test` (pps)                                                               | not built (Jane Street ladder is the shallow half only); covered by the same overlay                                                                                                  |
| `ppx_deriving.show`                                                                   | in the lock                                                                                                                                                                           |
| `ppx_deriving.eq` / `.ord`                                                            | override+: add the eq/ord plugin targets to the ppx_deriving override (same tarball as show)                                                                                          |
| `ppx_deriving_yojson`                                                                 | lock (dune project; runtime needs yojson, already locked)                                                                                                                             |
| `ppx_hash`, `ppx_sexp_conv`                                                           | in the lock (wave B)                                                                                                                                                                  |
| `lwt_ppx`                                                                             | lock (sublibrary of the locked lwt tarball; ppxlib is in the lock)                                                                                                                    |
| `pyro-caml-ppx`                                                                       | overlay: drop. Not an opam release; semgrep pins `git+https://github.com/semgrep/pyro-caml.git` (a Pyroscope profiling ppx). Out of the phase's scope; revisit when profiling matters |
| `collections`                                                                         | internal (translated, this directory)                                                                                                                                                 |
| `telemetry`, `parallelism`                                                            | internal (translated; closures dispatched below)                                                                                                                                      |
| `fpath`, `hex`, `uuidm`, `semver`                                                     | lock (small pure-OCaml)                                                                                                                                                               |
| `fmt`                                                                                 | lock (the real opam fmt; retires the vendored `third_party/fmt` eventually)                                                                                                           |
| `ocolor`, `ANSITerminal`                                                              | lock                                                                                                                                                                                  |
| `logs.threaded`                                                                       | override+: add the threaded target to the logs override (needs threads opam_dep)                                                                                                      |
| `alcotest`, `alcotest-lwt`                                                            | lock (test-only; needed because commons links them, not to run their tests)                                                                                                           |
| `testo`                                                                               | lock with a git pin (semgrep pins `git+https://github.com/semgrep/testo.git`); the lock supports url+sha pins                                                                         |
| `timedesc`                                                                            | lock (+ its deps: timedesc-tzdb, timedesc-tzlocal)                                                                                                                                    |
| `bos`                                                                                 | lock (topkg/Bünzli, override like cmdliner/logs; + rresult/astring/fpath/logs deps)                                                                                                   |
| `pcre` (in addition to pcre2)                                                         | lock (pcre-ocaml against a vendored PCRE1 C lib, the pcre2 pattern)                                                                                                                   |
| `digestif.ocaml`                                                                      | lock (the pure-OCaml implementation half; avoids the C variant's stubs)                                                                                                               |
| `sexplib`                                                                             | lock (sexplib0 is in; sexplib adds num? no, modern sexplib is pure + parsexp)                                                                                                         |
| `memtrace`                                                                            | lock (small, pure)                                                                                                                                                                    |
| `eio_main`                                                                            | lock, the heavyweight item (eio + eio_linux/eio_posix: uring on linux). Needs its own scoping pass                                                                                    |
| `str`, `unix` (stdlib)                                                                | already modeled by the driver                                                                                                                                                         |
| `re`, `yojson`, `atdgen-runtime`, `cmdliner`, `logs`, `uri`, `lwt`, `parmap`, `pcre2` | in the lock                                                                                                                                                                           |

`semgrep-interfaces` (semgrep fork, git pin) and the rest of semgrep.opam
stay out of scope until a translated dir actually names them.

## libs/telemetry + libs/parallelism rejection dispatch

Translating these two dirs deleted the four telemetry overlay stubs
(`Common_metrics.ml`, `Tracing.ml` in commons; `Logging.ml`,
`Process_limit_metrics.ml` in process_limits): the real sources compile, and
the commons dune overlay declares `telemetry`/`parallelism` while the
process_limits dune overlay is gone entirely (upstream translates as-is).
The dune stanzas decompose as (same legend as above):

| piece                                           | dispatch                                                                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `opentelemetry`, `opentelemetry-logs`           | lock, git pin: the semgrep fork (`semgrep/ocaml-opentelemetry`, commit from semgrep.opam's pin-depends) as one tarball entry with an override (bundled Atomic codegen, a `(select)` for the hmap key, inert promote-guarded proto rules). Pulls `pbrt` and `thread-local-storage` into the lock; the override also builds `opentelemetry.client`, `.proto`, `.atomic` and the `ambient-context{,.types,.eio}` chain |
| `opentelemetry-client-ocurl`                    | NOT built: curl C bindings (ocurl + ezcurl + a system libcurl). `Telemetry.ml` overlay fails loudly where the backend would be created                                                                                                                                                                                                                                                                              |
| `opentelemetry-client-cohttp-eio`               | NOT built: a full cohttp + tls-eio + mirage-crypto stack. Same overlay, same loud failure                                                                                                                                                                                                                                                                                                                           |
| `opentelemetry.client` (Self_trace)             | override target on the fork (cheap: mtime + pbrt); keeps the `Telemetry.ml` overlay delta to setup_otel only                                                                                                                                                                                                                                                                                                        |
| `ambient-context`, `ambient-context-lwt`        | lock (one tarball, override: virtual library + default_implementation + select + vendored Atomic codegen; pinned sha deviates from opam metadata, see the override header)                                                                                                                                                                                                                                          |
| `opentelemetry.ambient-context.eio`             | override target on the fork (eio fiber-local storage; eio already locked)                                                                                                                                                                                                                                                                                                                                           |
| `eio`, `eio.unix` (telemetry dune overlay adds) | in the lock; `Telemetry.mli`'s eio_sw_base type names them, upstream reached them through the cohttp-eio client                                                                                                                                                                                                                                                                                                     |
| `ptime.clock.os` (opentelemetry core dep)       | override+: ptime override grows the clock sublibrary (one C stub)                                                                                                                                                                                                                                                                                                                                                   |
| `eio_main`, `eio.mock` (parallelism)            | overlay: drop. No source references either (the one `Eio_main` mention is a doc-comment example in `Executor_pool.mli`); eio_main would pull the io_uring backend closure, still out of scope                                                                                                                                                                                                                       |
| `uri`, `yojson`, `logs`, `collections`, pps     | already in the lock / internal                                                                                                                                                                                                                                                                                                                                                                                      |

## path/parsing layer rejection dispatch (commons2, paths, gitignore, lib_parsing)

No new opam packages: every external name was already in the lock (fpath,
sexplib, logs, the deriving/hash/sexp rewriters and their runtimes). The
stanzas decompose as:

| piece                                                              | dispatch                                                                                                                                                                                                                                                                                      |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profiling.ppx` in pps (paths, gitignore, lib_parsing)             | internal rewriter, already translated; composes into the generated per-library ppx drivers                                                                                                                                                                                                    |
| `ppx_profiling` in lib_parsing's pps                               | was: overlay rename to the public name `profiling.ppx`. RETIRED by the atdgen-rule slice: SEMGREP_LIBS now keys internal rewriter names too (dispatch below), and the overlay keeps upstream's `ppx_profiling`                                                                                |
| `(inline_tests)` + `ppx_inline_test` (lib_parsing)                 | overlay: drop the stanza AND strip the let%test blocks in `Pos.ml` (the Ord.ml pattern; decided in the plan)                                                                                                                                                                                  |
| `git_wrapper` (lib_parsing)                                        | overlay: drop. No lib_parsing source references Git_wrapper (ocamldep confirms); keeping it would pull libs/git_wrapper and through it the ocaml-git closure (`git >= 3.18.0`: carton, decompress, checkseum, ...). Dispatched out of scope until a translated dir actually names Git_wrapper |
| `ppx_hash` / `ppx_sexp_conv` runtimes                              | in the lock (wave B); ppx_runtime tables propagate ppx_hash_lib / ppx_sexp_conv_lib / sexplib0                                                                                                                                                                                                |
| `fpath`, `sexplib`, `logs`                                         | in the lock                                                                                                                                                                                                                                                                                   |
| `commons`, `commons2`, `paths`, `glob`, `profiling`, `collections` | internal                                                                                                                                                                                                                                                                                      |

## src/ast_generic rejection dispatch

dune2bazel translates `src/ast_generic` at the pinned commit with no overlay:
every dune field is modeled. The stanza decomposes as (same legend):

| piece                                                     | dispatch                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `atdgen-runtime`, `sexplib`                               | in the lock                                                                                                                                                                                                                                                                                                                                                                                                             |
| `commons`, `lib_parsing`                                  | internal                                                                                                                                                                                                                                                                                                                                                                                                                |
| `ppx_deriving.show/.eq/.ord`, `ppx_hash`, `ppx_sexp_conv` | in the lock (waves A/B), already composing in commons' driver                                                                                                                                                                                                                                                                                                                                                           |
| `profiling.ppx`                                           | internal rewriter; the generated `Profiling.measure` calls resolve transitively (ast_generic does not name `profiling`, but the provider model propagates lib_parsing's includes, matching dune's implicit_transitive_deps)                                                                                                                                                                                             |
| `visitors.ppx`                                            | lock: visitors 20260520, the exact `=` pin in semgrep.opam. TRANSLATED (`src_dirs: runtime, src`; both stanzas are fully modeled, `(env ...)` is inert). A ppx_deriving-plugin-style rewriter (Pottier; gitlab.inria.fr archive like menhir)                                                                                                                                                                            |
| visitors' generated `VisitorsRuntime` references          | lock `ppx_runtime` table: `visitors.ppx -> visitors.runtime` (dune's ppx_runtime_libraries propagation, reproduced data-driven; ast_generic never names the runtime itself)                                                                                                                                                                                                                                             |
| visitors' `ppxlib >= 0.37.0` floor                        | NOT honored: the lock stays at ppxlib 0.36.0. The floor guards the 2025/11/14 type-annotation feature against older ppxlib; the harness compiled visitors 20260520 against 0.36.0 and drove the composed driver over AST_generic.ml cleanly. semgrep.opam pins ppxlib `= 0.37.0`; bumping the lock's ppxlib (override rework + Jane Street re-check) is dispatched to whenever a translated dir actually breaks on 0.36 |
| `tests/` subdir                                           | not translated: its own dune (alcotest consumer), and the non-recursive source glob keeps it out of `:ast_generic`                                                                                                                                                                                                                                                                                                      |

## languages/go rejection dispatch (ast, tree-sitter, generic)

The first source-to-generic-AST chain. All three dirs translate as-is (no
overlays); every `(libraries ...)` name was already resolvable:

| piece                                                              | dispatch                                                                                                                                                |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ast_generic`, `commons`, `lib_parsing`, `lib_parsing_tree_sitter` | internal                                                                                                                                                |
| `parser_go.ast` (tree-sitter, generic deps)                        | internal (translated, this slice); the lib_map keys the dotted public names                                                                             |
| `tree-sitter-lang.go`                                              | in the lock (wave C grammar stamp, examples/treesitter_go)                                                                                              |
| `ppx_deriving.show`                                                | in the lock                                                                                                                                             |
| `languages/go/menhir` (the legacy yacc parser)                     | NOT translated: out of scope (the tree-sitter path is the one semgrep-core exercises for Go); revisit only if a translated dir names `parser_go.menhir` |
| `semgrep-go/` (submodule dir under tree-sitter)                    | inert: the shallow clone does not init submodules; the grammar rides the lock (tree-sitter-go) and the non-recursive glob never descends                |

## src/configuring + internal rewriters dispatch (the atdgen-rule slice)

The slice that opened the semgrep-core closure. Three dirs landed
(libs/commons/ppx, libs/telemetry/ppx, src/configuring) plus one translator
feature and one resolution mechanism:

| piece                                                          | dispatch                                                                                                                                                                                                                                                                                          |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `(rule ... (action (run atdgen ...)))` pair (Rule_options.atd) | translator feature: dune2bazel models exactly this rule shape (explicit targets/deps, literal flags, trailing `%{deps}`) as a genrule over the locked atdgen, the shape the ocaml-tree-sitter-core override hand-writes; generated sources join the library srcs. Any other (rule) rejects loudly |
| dune-internal pps names (`ppx_profiling` in configuring's pps) | SEMGREP_LIBS carries BOTH the public name and the dune `(name ...)` for internal rewriters (upstream uses them interchangeably: src/core says `commons.ppx` AND `ppx_telemetry`). Retired the lib_parsing overlay's rename-to-public-name, which this mechanism replaces                          |
| `commons.ppx` / `telemetry.ppx`                                | internal (translated, kind ppx_deriver, the profiling/ppx shape); telemetry/ppx's ppx_tests subdir has its own dune and stays out via the non-recursive glob                                                                                                                                      |
| `base64` (src/sca's one new external)                          | lock, override: src/dune filters `(modules unsafe base64)` and copies unsafe.ml from a `%{read:...}` config probe, provably unsafe_stable.ml on the 5.3 sysroot (the parmap pattern)                                                                                                              |
| `atdgen-runtime`, `commons`, `ppx_deriving_yojson`, ...        | in the lock / internal                                                                                                                                                                                                                                                                            |

## semgrep_core closure frontier dispatch (spacegrep .. src/target)

The slice that carried the frontier to src/core's doorstep. Eight dirs
landed (src/spacegrep/src/lib, languages/javascript/ast, src/aliengrep,
cli/src/semgrep/semgrep_interfaces, src/rule, src/sca, libs/git_wrapper,
src/target), one translator feature, and one resolution extension. No new
lock entries:
every external name (ANSITerminal, pcre, semver, base64, hex,
atdgen-runtime, the deriving/hash/sexp/yojson rewriters) was already in,
though two existing entries had to bump (next table).

| piece                                            | dispatch                                                                                                                                                                                                                                                                            |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `(modules ...)` (semgrep_interfaces)             | translator feature: a list that provably equals the dir's globbed module set (mli-only modules count; atdgen-rule outputs count) validates and drops -- the BUILD glob takes the whole dir anyway. Any mismatch is real module filtering and rejects loudly (base64's filtered list stays an override) |
| `(libraries alcotest)` (aliengrep)               | overlay: drop. No aliengrep source references Alcotest (ocamldep re-measured at the pin); its tests subdir has its own dune. The git_wrapper pattern                                                                                                                                |
| `Alcotest.testable` in src/sca (Dependency)      | overlay: strip kind_testable from Dependency.ml{,i}. Upstream compiles it because commons links alcotest transitively; our commons overlay measured alcotest out, and nothing in the tree references kind_testable (grep at the pin), so locking alcotest to link one dead value stays out of scope |
| `semgrep_core_rule` / `semgrep_core_target` refs | SEMGREP_LIBS now keys non-rewriter libraries by BOTH names too: src/core's stanza uses the dune (name ...) while src/sca / src/target use the semgrep.* public names (the mechanism the atdgen-rule slice introduced for rewriters)                                                 |
| `Cmdliner`, `Digestif` in spacegrep/rule sources | resolve transitively through commons (dune's implicit_transitive_deps; the provider model propagates includes), no stanza change needed                                                                                                                                             |
| `Git_wrapper` in src/target (Origin, Target)     | the moment the lib_parsing dispatch deferred to: a translated dir finally names Git_wrapper. libs/git_wrapper translates with overlays instead of locking ocaml-git (`git >= 3.18.0` would pull carton, decompress, checkseum, ...): the dune drops `git`; `hash` keeps upstream's own equation (`Digestif.SHA1.t`, digestif already locked, reached transitively through commons exactly as upstream does); `commit`/`author` mirror ocaml-git 3.x's record shapes field for field so future consumers (src/reporting reads `(commit_author c).date`) stay source-compatible; `blob` is the raw contents (blob_digest computes the real `blob <len>\0` object id); the in-memory object-store walkers (`commit_blobs_by_date`, `commit_digest`) fail loudly at runtime, the Telemetry.ml dispatch. The git-CLI shell-out layer, which is all src/target reaches, is untouched. src/target's dune overlay then declares the dep where the references live (the measured-set rule; upstream got it transitively from lib_parsing) |
| `rule_schema_v2.atd` in src/rule                 | inert: no `(rule)` stanza generates from it at this pin and no source references Rule_schema_v2_t (the env-stanza comment upstream is stale); the .atd never joins the source glob                                                                                                  |
| `(env ...)` blocks (src/rule, src/target)        | inert today: the env stanza is dropped and warnings are not errors in our build                                                                                                                                                                                                     |
| `Atdgen_runtime.Yojson_extra` (interfaces codegen) | lock bump: atd 2.16.0 -> 3.0.1. The checked-in `_j` codegen calls the 3.x runtime (semgrep-interfaces.opam floors atdgen >= 3.0.1; semgrep.opam >= 3.0.0); the override grows a `*.mll` glob for the runtime's new ocamllex modules, everything else transfers                      |
| atdgen-runtime 3.0.1 floors `yojson >= 3.0.0`    | lock bump: yojson 2.2.2 -> 3.0.0. The lib/ tree and build structure are identical (the only dune delta drops the inert `(libraries seq)`); the mucppo override transfers unchanged. ppx_deriving_yojson 3.10.0's runtime floor is yojson >= 1.6.0, still satisfied                  |

## yaml closure dispatch (the slice that closed semgrep_core)

`src/core` named `yaml` (semgrep.opam floor >= 3.2.0); everything else in
its stanza already resolved. ocaml-yaml 3.2.0 is a two-stage **ctypes
stubgen** package, landed as three lock entries:

| piece                                       | dispatch                                                                                                                                                                                                                                                                                                          |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `integers` 0.8.0                            | lock, override: `(install_c_headers ...)` + `(c_names ...)` (the pre-foreign_stubs stub spelling) are not modeled; one flat library, the parmap c_srcs shape. integers.top (byte-only toplevel printers) stays unbuilt                                                                                            |
| `ctypes` 0.24.0                             | lock, override: ctypes_primitives.ml comes from a dune-configurator probe and ocaml_integers.h from a `%{lib:integers:...}` copy rule. The probe RUNS in a genrule (the lwt.unix discover mold) instead of being pinned like parmap's: its output is ~130 generated lines, and it is provably identical on both platforms (linux x86_64/aarch64 are little-endian LP64; long double is 16/16 on both). ctypes.stubs is a plain second library; ctypes-foreign (libffi) and ctypes.top stay unbuilt |
| ctypes' `bigarray` dep                      | inert: Bigarray is stdlib on OCaml 5.3, no archive or flag needed (verified in the harness)                                                                                                                                                                                                                       |
| `bigarray-compat`                           | never materialized: ctypes 0.24.0 dropped it (the scoping pass predicted it "likely"); opam resolution for yaml 3.2.0 pulls exactly ctypes + integers                                                                                                                                                             |
| `yaml` 3.2.0                                | lock, override: the six-piece dispatch below, landed as recorded                                                                                                                                                                                                                                                  |

The yaml override (`opam/overrides/yaml/BUILD.tpl`), as landed:

1. `vendor/` (`yaml.c`): the vendored libyaml C sources as a **cc_library**
   via cc_deps (the pcre2-c pattern won over c_srcs: stage one needs the
   header dir as a plain `-I`, and the stub compile needs `<yaml.h>` to
   resolve through an exported include dir), compiled `-DHAVE_CONFIG_H`
   against the checked-in config.h. dumper.c stays out, as upstream's dune
   leaves it.
2. `config/discover.ml`'s outputs are pinned (the parmap pattern):
   `cflags` is ocamlopt_cflags ("-O2 -fno-strict-aliasing -fwrapv", inlined
   into yaml_c's copts; the ppc64/msvc branches are dead) and
   `ctypes-cflags` is -I<installed ctypes headers>, which here is the
   staged `@ocaml_ctypes//:c_headers` filegroup.
3. `yaml.bindings.types`, `yaml.bindings`: plain ocaml_libraries over
   ctypes.stubs + ctypes (hand-written in the override; no codegen).
4. types/stubgen, stage one (the compile-AND-RUN genrule): run
   `ffi_types_stubgen.exe` to emit C, compile that C with the executor's
   gcc against the staged ctypes headers + vendor/yaml.h + the sysroot's
   `lib/ocaml` (dune's `%{ocaml_where}`), run the result to emit `g.ml`
   for `yaml.types`. One genrule, two process generations.
5. ffi/stubgen, stage two: `ffi_stubgen.exe -ml` -> `g.ml` and `-c` ->
   `yaml_stubs.c` for `yaml.ffi` (pure prints, no C compile in the
   genrule). The generated C quote-includes "ctypes_cstubs_internals.h"
   (staged by basename via c_headers) and angle-includes `<yaml.h>`
   (resolved through yaml_c's exported vendor/ dir).
6. `yaml` (lib/): plain library over yaml.ffi; `yaml.unix` and `yaml-sexp`
   stay unbuilt (src/core names only `yaml`).

Both stubgen binaries are exec-config tools running on the default pool, and
the stage-one genrule references the unconstrained `toolchain:ocaml_compiler`
exactly like yojson's ocamllex run (see bazel/ocaml/README.md, multi-arch
notes); g.ml's constants (libyaml struct sizes/offsets, enum values) agree
between the two LP64 platforms. integers/ctypes use c_srcs, not cc_library,
so they live in the both-arch `ladder_builds`; yaml's cc_library puts it
(and src/core, which also reaches commons' pcre) in `ladder_builds_cc`.

The whole stack was pre-validated in the local harness: every override
recipe replayed through the real driver (including both stubgen genrules),
an e2e binary parsed and re-emitted YAML through the driver-built chain,
and src/core compiled with its full composed ppx driver against it.

## src/parsing scoping (recorded, NOT landed)

`src/parsing` (Parse_target/Parse_pattern; `semgrep.parsing`, dune name
`semgrep_parsing`) is the next consumer: its stanza names `semgrep_core`
plus essentially the whole parser matrix. Its two `Parsing_stats.atd`
atdgen rule pairs match the translated rule shape, and its pps line
(ppx_profiling, ppx_deriving.show, telemetry.ppx) already resolves. The
`(libraries ...)` decompose as:

| group                                                                                  | dispatch                                                                                                                                                                                                                                                                                          |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pcre`, `base`, `uri`                                                                  | in the lock                                                                                                                                                                                                                                                                                       |
| `commons`, `git_wrapper`, `paths`, `lib_parsing`, `process_limits`, `spacegrep`, `parallelism`, `semgrep_core` | internal, translated                                                                                                                                                                                                                                                                              |
| `parser_go.tree_sitter` / `.ast_generic`, `parser_javascript.ast`                      | internal, translated (the Go chain + JS AST)                                                                                                                                                                                                                                                      |
| `fast_json` (libs/fast_json)                                                           | internal, new dir: lib_parsing + paths + yojson + ast_generic, pure, no pps surprises. Cheapest first landing                                                                                                                                                                                     |
| `semgrep.typing` (src/typing)                                                          | internal, new dir: commons + lib_parsing + parallelism + semgrep_core + ppx_deriving.runtime. Cheap                                                                                                                                                                                               |
| `parser_yaml.ast/.parser/.ast_generic` (languages/yaml/)                               | internal, new dirs: the first yaml-lock consumers outside src/core (parser names `yaml` directly)                                                                                                                                                                                                 |
| `pfff_lang_GENERIC_analyze` (src/analyzing)                                            | internal, new dir: names `semgrep.il`, pulling src/il (its own scoping look)                                                                                                                                                                                                                      |
| `semgrep.prefiltering` (src/prefiltering)                                              | internal, new dir: two more atdgen rule pairs (translated shape)                                                                                                                                                                                                                                  |
| `semgrep_targeting` (src/targeting)                                                    | internal, new dir, the first NEW external since yaml: `ppx_blob` in pps (needs a lock entry) plus `(preprocessor_deps (file default.semgrepignore))`, which the translator does not model -- expect a translator feature or an override-style dispatch                                            |
| `pfff-lang_GENERIC-naming` (src/naming)                                                | internal, new dir: commons + ast_generic + semgrep.core + semgrep.typing + parser_javascript.ast                                                                                                                                                                                                  |
| `ojsonnet` (libs/ojsonnet)                                                             | internal, new dir: names `parser_jsonnet.tree_sitter`, so the jsonnet grammar must stamp into the lock first (the tree-sitter-go pattern)                                                                                                                                                         |
| tree-sitter language chains (python, cpp, php, ocaml, typescript, scala, bash, dockerfile, java, jsonnet, terraform, ruby, ql, lisp) | each needs its grammar stamped from the submodule commit + the ast/tree-sitter/generic dirs translated; bash's grammar is already locked. scala's path is `recursive_descent` (plain OCaml, no grammar)                                                                                            |
| direct-to-generic parsers (dart, cairo, solidity, csharp, rust, lua, kotlin, swift, julia, r, hack, fga, html, promql, protobuf, move_on_sui, move_on_aptos, circom) | `.ast_generic`-only names, but most wrap a tree-sitter CST under the hood -- scope each dir before assuming it is grammar-free                                                                                                                                                                    |
| `parser_*.menhir` (go, ocaml, python, cpp, php, javascript, json)                      | the wrinkle: the legacy menhir parsers are named DIRECTLY in src/parsing's stanza, so "menhir parsers stay out" requires either landing them or measuring them out per dir. parser_json.menhir (which itself depends on parser_javascript.menhir) is the JSON path Parse_target actually uses, so the JSON menhir chain likely must land; the rest are overlay candidates after an ocamldep measurement |

Suggested landing order: fast_json + typing (cheap, no new externals); the
languages/yaml trio (validates the yaml lock from a second consumer);
src/il + analyzing; prefiltering; targeting (ppx_blob lock +
preprocessor_deps dispatch); naming; ojsonnet + the jsonnet grammar; then
the per-language matrix in waves; the menhir question last, measured, with
src/parsing itself closing the slice.
