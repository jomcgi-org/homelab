# Phase 9: src/parsing closure Implementation Plan

> **For Claude:** Execute wave-by-wave with subagent-driven development;
> every increment ends CI-green before the next starts. One PR per wave.

**Goal:** Carry the translated Semgrep CE frontier from `src/core`
(`semgrep_core`, green today) up to `src/parsing` (`Parse_target` /
`Parse_pattern`, dune name `semgrep_parsing`), then to the `semgrep-core`
executable. `src/parsing` names essentially the whole parser matrix, so the
climb is a sequence of bottom-up dir landings plus tree-sitter grammar stamps,
exactly the cadence the opam universe and the wave-D frontier already used.

**Parent:** continues `2026-06-10-ocaml-rules-semgrep-scale.md` (Phase 8) and
`2026-06-12-ocaml-semgrep-tree-entry.md` (wave D). The frontier mechanics,
verification model, and reject-loudly contract are inherited unchanged. The
authoritative scoping for this phase is the "src/parsing scoping (recorded,
NOT landed)" section of `bazel/ocaml/semgrep_src/README.md`; this plan turns
that section's suggested landing order into bite-sized, CI-gated waves.

**Decisions of record:** ADR tooling/004 (scale the custom ruleset), 005
(engine shape), 006 (multi-arch registry), 008 (distribution; macOS and
cross-compilation out of scope).

---

## How a dir lands (the mechanical contract, unchanged from wave D)

Every "translate dir X" task is the same shape:

1. Append the dune dir path to `SEMGREP_SRC_DIRS` in
   `bazel/ocaml/semgrep_src/source.bzl`, in dependency order (after everything
   it names).
2. Add its library to `SEMGREP_LIBS`, keyed by BOTH the dune `(name ...)` and
   the public name when they differ (upstream pps and `(libraries ...)` lines
   use them interchangeably).
3. If translation rejects, dispatch each rejection by the legend: **lock** (new
   opam entry in `lock.json` via `update_lock.py`), **override+** (extend an
   existing `opam/overrides/<name>`), **overlay** (source patch under
   `semgrep_src/overlays/`, added to `OVERLAYS`), or **internal** (another
   `SEMGREP_SRC_DIRS` entry it depends on, landed first).
4. Wire the new `@semgrep_src//:target` into the right build_test in
   `bazel/ocaml/examples/opam_ladder/BUILD`: `ladder_builds` for pure targets,
   `ladder_builds_cc` for anything whose dep closure pulls a `cc_library`
   (e.g. anything reaching `commons`' pcre, or `yaml`).
5. Push, watch CI via `mcp__buildbuddy__get_invocation` (commitSha selector).
   Quote failures verbatim before hypothesizing. Update `semgrep_src/README.md`
   (the "Translated today" table and any new dispatch table) in the same PR.

**Verification model:** unchanged. No local `bazel test`. Pre-validate override
and overlay recipes in the local driver harness where possible
(`driver/ocaml_compile.sh --sysroot-tar` runs standalone), then push. Small
Conventional Commits; the arm64 shard stays green (new opam packages land in
`examples/opam_ladder` which the shard builds for both arches; grammar examples
stay tagged `no-arm64` until the C++ toolchain question is resolved).

**Reject loudly stays the contract:** silent mistranslation is the only
unacceptable failure mode. Every rejection names where the work lands.

---

## Wave 1: fast_json + typing (PR `feat/ocaml-semgrep-parsing-w1`)

The cheapest first landing per the README: pure dirs, no new lock entries, no
pps surprises. Validates that the post-`src/core` climb moves.

### Task 1.1: libs/fast_json

`(libraries ...)` = lib_parsing + paths + yojson + ast_generic, all already
locked or translated. Translate as-is; expect a clean run. Target
`:fast_json` (+ public name if the dune name differs); wire into
`ladder_builds` unless its closure reaches a cc_library (it reaches commons via
lib_parsing, so it most likely belongs in `ladder_builds_cc`: confirm from the
first CI run and place accordingly).

### Task 1.2: src/typing

`semgrep.typing` (dune name `semgrep_typing`): commons + lib_parsing +
parallelism + semgrep_core + ppx_deriving.runtime, all resolved. Translate
as-is.

### Task 1.3: phase gate

Push, PR, CI green on both arches, README table updated, one comprehensive
review. Acceptance: `:fast_json` and `:semgrep_typing` build on RBE.

---

## Wave 2: languages/yaml trio (PR `feat/ocaml-semgrep-parsing-w2`)

The first consumers of the `yaml` lock outside `src/core`; landing them
validates the two-stage ctypes-stubgen yaml override from a second caller,
which de-risks every later yaml-touching dir. Three dirs, dependency-ordered:

### Task 2.1: languages/yaml/ast -> parser_yaml.ast
### Task 2.2: languages/yaml/parser -> parser_yaml.parser

Names `yaml` directly (the lock entry), plus the ast dir. This is the
validation point: if the yaml override only ever linked through `src/core`'s
single use, a second consumer surfaces any latent staging gap. Quote any
linker error before touching the override.

### Task 2.3: languages/yaml/generic -> parser_yaml.ast_generic
### Task 2.4: phase gate

All three into `ladder_builds_cc` (yaml's cc_library). Acceptance: a small
example parsing a YAML snippet through `parser_yaml` into the generic AST,
green on RBE. No grammar is involved, so target both arches.

---

## Wave 3: src/il + src/analyzing (PR `feat/ocaml-semgrep-parsing-w3`)

### Task 3.1: src/il -> semgrep.il

`src/analyzing` names `pfff_lang_GENERIC_analyze`, which names `semgrep.il`,
so `src/il` lands first. Scope `src/il`'s own `(libraries ...)` at
implementation start (its dune was not pre-decomposed in the README); dispatch
each name by the standard legend before writing the task list.

### Task 3.2: src/analyzing -> pfff_lang_GENERIC_analyze
### Task 3.3: phase gate

Acceptance: both build on RBE; README tables updated with the `src/il`
decomposition discovered in 3.1.

---

## Wave 4: src/prefiltering (PR `feat/ocaml-semgrep-parsing-w4`)

### Task 4.1: src/prefiltering -> semgrep.prefiltering

Two more atdgen `(rule)` pairs, matching the already-translated genrule shape
(`src/configuring` set the pattern). No new externals expected.

### Task 4.2: phase gate

Acceptance: green on RBE; the atdgen pairs translate without a new override.

---

## Wave 5: src/targeting (PR `feat/ocaml-semgrep-parsing-w5`)

The first wave with genuinely NEW translator/lock work since yaml. Two new
problems, each its own task:

### Task 5.1: ppx_blob lock entry

`src/targeting`'s pps line names `ppx_blob` (embeds a file as a string at
compile time). Add the lock entry (dune project; ppxlib in the lock). It is a
ppx rewriter with a runtime; follow the ppx_deriving lock+override shape. A
small `examples/` test embedding a file via `[%blob "path"]` proves it before
the real consumer.

### Task 5.2: preprocessor_deps dispatch

`src/targeting`'s dune has `(preprocessor_deps (file default.semgrepignore))`,
which the translator does not model: the named file must be staged into the ppx
action's inputs so `[%blob "default.semgrepignore"]` can read it at preprocess
time. Decide between (a) a translator feature: map `(preprocessor_deps (file
X))` to extra `data`/inputs threaded into the `preprocess` action, or (b) an
override-style dispatch for this one dir. Recommend (a), the translator
feature, since `ppx_blob` + `preprocessor_deps` recur in later dirs; state the
one-sentence justification if choosing (b) instead. This is the genuine design
choice in this phase: surface the option to Joe before implementing if (a)
proves more than a localized change to the `preprocess` plumbing.

### Task 5.3: src/targeting -> semgrep_targeting
### Task 5.4: phase gate

Acceptance: `:semgrep_targeting` builds with the file embedded; the ppx_blob
example test is green.

---

## Wave 6: src/naming (PR `feat/ocaml-semgrep-parsing-w6`)

### Task 6.1: src/naming -> pfff-lang_GENERIC-naming

commons + ast_generic + semgrep.core + semgrep.typing + parser_javascript.ast,
all translated by now. Expect a clean translate.

### Task 6.2: phase gate

Acceptance: green on RBE.

---

## Wave 7: ojsonnet + the jsonnet grammar (PR `feat/ocaml-semgrep-parsing-w7`)

**Re-plan checkpoint: scope the jsonnet grammar stamp before starting.**

### Task 7.1: stamp tree-sitter-jsonnet into the lock

`libs/ojsonnet` names `parser_jsonnet.tree_sitter`, so the jsonnet grammar must
be stamped first, following the tree-sitter-go/-bash pattern (`opam: false`
lock entry pinned to the commit the `languages/jsonnet` submodule references).
Grammar examples stay `no-arm64` until the C++ toolchain question is resolved.

### Task 7.2: languages/jsonnet ast/tree-sitter/generic dirs
### Task 7.3: libs/ojsonnet -> ojsonnet
### Task 7.4: phase gate

Acceptance: an example parsing a jsonnet snippet through the grammar; ojsonnet
builds on RBE (x86_64).

---

## Wave 8: the per-language tree-sitter matrix (multiple PRs)

**Re-plan checkpoint: this wave gets its own sub-plan; expand at the checkpoint
into one PR per small batch of languages.**

Each language in src/parsing's stanza needs its grammar stamped from the
submodule commit plus its `ast` / `tree-sitter` / `generic` dirs translated,
the parser_go chain being the worked template. The set, from the README:

- **Grammar-backed (stamp + 3 dirs each):** python, cpp, php, ocaml,
  typescript, scala (note: scala's path is `recursive_descent`, plain OCaml, no
  grammar), bash (grammar already locked), dockerfile, java, jsonnet (landed in
  wave 7), terraform, ruby, ql, lisp.
- **Direct-to-generic parsers (`.ast_generic`-only names):** dart, cairo,
  solidity, csharp, rust, lua, kotlin, swift, julia, r, hack, fga, html,
  promql, protobuf, move_on_sui, move_on_aptos, circom. Most wrap a tree-sitter
  CST under the hood: scope each dir for a hidden grammar before assuming it is
  grammar-free.

Land in batches of two or three related languages per PR (e.g. the C-family,
then the JVM-family), each batch its own example parsing a real snippet. Order
within the wave is not load-bearing except where one language's generic dir
reuses another's; measure with the translator's reject output.

**arm64 note:** every grammar example stays `no-arm64` tagged until the C++
toolchain question (Phase 7 carry-over) is resolved. Resolving it is a
prerequisite for arm64 `semgrep-core`, tracked separately; it does not block
the x86_64 climb.

---

## Wave 9: the menhir question, then src/parsing itself (PR(s) `feat/ocaml-semgrep-parsing-w9`)

**Re-plan checkpoint: measure before landing.**

src/parsing's stanza names the legacy menhir parsers DIRECTLY
(`parser_*.menhir` for go, ocaml, python, cpp, php, javascript, json), so
"menhir parsers stay out" is not free here:

### Task 9.1: ocamldep measurement

For each `parser_*.menhir`, measure (ocamldep over the menhir-generated
sources) whether `Parse_target` actually reaches it. Known result from the
README: `parser_json.menhir` (which itself depends on `parser_javascript.menhir`)
is the JSON path `Parse_target` uses, so the JSON menhir chain likely must
land; the rest are overlay-out candidates.

### Task 9.2: land the JSON menhir chain

`parser_javascript.menhir` then `parser_json.menhir` (menhir codegen is
already modeled, Phase 5 / `libs/glob` exercised it). The other six menhir
parsers get an overlay dropping them from src/parsing's `(libraries ...)`,
each overlay documenting the ocamldep evidence it was unreferenced.

### Task 9.3: src/parsing -> semgrep_parsing
### Task 9.4: phase gate

Acceptance: `:semgrep_parsing` builds on RBE with the measured parser set; the
overlay-dropped menhir parsers are documented in the README dispatch table.

---

## Wave 10: the semgrep-core binary (PR `feat/ocaml-semgrep-core-bin`)

**Re-plan checkpoint: scope the entry-point dir before starting; it is the
headline goal and may surface a final tranche of src/ dirs (matching,
engine, reporting, ...) between src/parsing and the executable.**

The `semgrep-core` executable links the engine on top of `semgrep_parsing`.
Map the dune `(executable)` / `(test)` stanzas to `ocaml_binary` / `ocaml_test`
(Phase 5 model). Expect new src/ dirs named by the binary's stanza (matching
engine, output/reporting) each scoped and landed by the same contract before
the final `ocaml_binary` target. Acceptance (the phase's headline metric): the
`semgrep-core` binary links on RBE (x86_64) and runs a trivial
`semgrep-core --help` or a single-rule scan over a fixture in an `ocaml_test`.

---

## Standing rules

Inherited: Conventional Commits, reject-loudly, README discipline
(`semgrep_src/README.md` and `bazel/ocaml/README.md` updated in the same PR as
the semantics they describe), one comprehensive review per PR, arm64 shard
stays green, never blame infra without a ruled-out test failure. The
suggested wave order is the README's; reorder only with a one-sentence
justification when CI teaches us a cheaper path.
