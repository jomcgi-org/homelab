# Semgrep Tree Entry (Phase 8) Implementation Plan

> **For Claude:** Execute task-by-task with subagent-driven development or
> directly; every increment ends CI-green before the next starts.

**Goal:** Enter the Semgrep CE source tree: grow the pinned opam universe to
Semgrep's direct dependencies, build the shallow Jane Street ladder, stamp the
per-language tree-sitter grammars, and translate `libs/` bottom-up with
dune2bazel, until `libs/commons` + `libs/glob` + one tree-sitter language build
end to end on RBE.

**Parent:** Phase 8 of `2026-06-10-ocaml-rules-semgrep-scale.md` (Phases 2-7
merged and green). Decisions of record: ADR tooling/004 (custom ruleset),
005 (engine shape), 006 (multi-arch registry), 008 (distribution; macOS and
cross-compilation explicitly out of scope here).

**Verification model:** unchanged from the parent plan. No local `bazel test`.
Sanity-check increments in the local harness against the pinned sysroot where
possible (`driver/ocaml_compile.sh --sysroot-tar` runs standalone), then push,
watch CI via `mcp__buildbuddy__get_invocation` (commitSha selector), quote
failures verbatim before hypothesizing. Small Conventional Commits; one PR per
coherent chunk; the arm64 shard from Phase 7 must stay green (new opam packages
land in `examples/opam_ladder`, which the shard builds for both arches).

**Decided, do not re-litigate:**

- Jane Street ladder is the **shallow half only**, through ppx_hash:
  `ocaml_intrinsics_kernel`, `base`, `ppx_compare`, `ppx_sexp_conv`,
  `ppx_hash`. Do NOT build `time_now` / `jst-config` / `ppx_inline_test`.
  Semgrep's 5 `inline_tests` dune files are overlay-patched instead, and the
  `let%test` blocks must be removed from the `.ml` sources, not just the
  stanza: without the ppx rewriter the syntax does not even parse.
- Translator contract stays **reject loudly**; every rejection names where the
  work lands (translator feature, override, or source patch).

---

## Wave A: lock growth toward Semgrep's direct deps

Semgrep CE's opam file names ~75 direct deps. We do not add all 75 up front;
each increment adds a dependency-ordered slice, extends
`examples/opam_ladder:ladder_builds`, and must be CI-green before the next.
`update_lock.py --add name==version` maintains `lock.json` from a workstation
(network); versions are chosen to satisfy Semgrep CE's constraints at pin time.

Known build-system split, which decides translated-vs-override per package:

- **dune projects** (translator path, override only if self-bootstrapping):
  lwt (+ ocplib-endian), uri (+ stringext, angstrom, bigstringaf), ocamlgraph,
  parmap (C stubs via the Phase 6 `c_srcs`/`cc_deps` machinery), alcotest later
  if needed.
- **non-dune (topkg/b0) projects, mostly Bünzli**: cmdliner, logs, astring,
  fmt (the real opam fmt, retiring the vendored `third_party/fmt` eventually).
  dune2bazel cannot read these (no dune files); each gets a small
  `opam/overrides/<name>/BUILD.tpl`, which their flat module layout makes easy.

### Task A1: cmdliner + logs (override increment)

The two most load-bearing non-dune leaves. Add lock entries, write override
BUILDs (flat `ocaml_library`, `wrapped` per upstream install layout), extend
`ladder_builds`, push, CI green on both arches.

### Task A2: lwt (translated increment)

`ocplib-endian` first (dune, translated), then `lwt` (dune; its dune tree uses
a configure-discovery step for libev that we pin off: no libev, vanilla unix
engine). Expect translator gaps (lwt uses `(select ...)` clauses); extend the
translator or override `lwt.unix` only, keeping core `lwt` translated.

### Task A3: uri + ocamlgraph

`stringext`, `bigstringaf` (C stubs), `angstrom`, `uri`; `ocamlgraph`
standalone. All dune. Acceptance: ladder green including the new entries.

### Task A4: parmap + remaining direct-dep slice

`parmap` (C stubs) plus the next slice the `libs/commons` translation run
(Wave D) names as missing. This task is deliberately elastic: Wave D's
rejection output is the authoritative shopping list, so A4 repeats until
`libs/commons`' opam closure is in the lock.

## Wave B: Jane Street shallow ladder

### Task B1: ocaml_intrinsics_kernel + base

`base` is the heavy one (C stubs `c_srcs`, large module set, dune). Translate;
override only if its dune tree resists (it uses few exotic stanzas at v0.17).

### Task B2: ppx_compare, ppx_sexp_conv, ppx_hash

Each is a ppxlib rewriter library + runtime lib; the Phase 4 `ocaml_ppx` /
`preprocess` model fits directly. Acceptance: an `examples/` test deriving
`compare`/`sexp_of`/`hash` end to end, both arches.

## Wave C: tree-sitter grammar repos

Stamp per-language grammar lock entries (`opam: false`, the
`tree-sitter-json` override as template), pinned to the grammar commits
Semgrep's `languages/` submodules reference. First target language only (the
one Wave D's chosen first parser needs, likely `tree-sitter-go` or
`tree-sitter-python`); the rest are mechanical repeats later. Grammar examples
stay tagged `no-arm64` until the C++ toolchain question is resolved (Phase 7
note).

## Wave D: Semgrep source entry

### Task D1: pin @semgrep_src

Module extension cloning the pinned Semgrep CE commit (same shape as
`@ocaml_source`); a chosen-at-implementation commit on `develop`, recorded with
the pin rationale.

### Task D2: libs/commons via dune2bazel

Run the translator over `libs/commons` (the root of the internal dep graph).
Every rejection gets a named decision. Known remainder needing hand overlays
or source patches, expected from the Phase 5 inventory:

- `inline_tests` x5: overlay-patch dune AND strip `let%test` blocks from the
  `.ml` sources (decided above).
- first-party `(rule)` stanzas: translate as genrules where inputs/outputs are
  explicit; reject anything unprovably hermetic.
- `(test)` / `(executable)` stanzas: `ocaml_test` / `ocaml_binary` mapping.
- virtual libraries / implementations: not modeled; override with the concrete
  implementation Semgrep actually selects.

### Task D3: libs/glob + first tree-sitter language

Acceptance for the phase (parent plan's success metric): `libs/commons`,
`libs/glob`, and one tree-sitter language parser build and their tests pass on
RBE. `semgrep-core` is the headline goal after that, out of this plan's scope.

---

## Standing rules

Inherited from the parent plan: Conventional Commits, reject-loudly, README
discipline (`bazel/ocaml/README.md` updated in the same PR as semantics
changes), one comprehensive review per PR, never blame infra without a
ruled-out test failure.
