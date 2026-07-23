# ADR 005: tOyCaml -- a representative demonstrator for the OCaml ruleset

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-06-10

---

## Problem

ADR 004 commits to scaling `bazel/ocaml` toward building a real OCaml analysis
engine, with the public Semgrep engine (`github.com/semgrep/semgrep`) as the
reference target. The scaling plan's acceptance tests, however, are generic
(`examples/hello`, `examples/regex`, `examples/c_stubs`). Generic examples do
not exercise the build features a real engine actually needs, and they hide
ordering constraints between those features (for instance, that a codegen tool
must work before any tree-sitter binding compiles).

We want a target that is:

1. **Representative** -- it mirrors the *shape* of how the public engine uses
   the OCaml ecosystem (a generic AST, lexing/parsing, pattern matching with
   metavariables, C stubs, a real opam dependency), so each ruleset capability
   is proven against something engine-like.
2. **Small and green** -- it builds in seconds on today's ruleset and stays
   green as features land, so it can be the standing acceptance target.
3. **Reference-free in the toy itself** -- the example code names no product;
   the mapping to the public engine lives here in the ADR.

The gap between "builds the toy" and "builds the engine" is a fixed list of
build features, all visible in the public repo. tOyCaml exists to drive them
out one at a time.

## Decision

Add **tOyCaml** at `bazel/ocaml/examples/toycaml`: a deliberately tiny "grep for
code" (parse a pattern and a target expression; structurally match the pattern,
with metavariables, against the target). It is wired to mirror the public
engine's ecosystem usage and grows feature-by-feature to cover the load-bearing
build features that engine requires. **JS/WASM is out of scope** (see below).

tOyCaml becomes the ruleset's representative acceptance target: as each new
capability lands, the matching toy component is upgraded to use it, so the
ruleset always has an engine-shaped, green target to build against. Phase 8 of
the scaling plan (the real engine) then reduces to "tOyCaml, but with hundreds
of opam packages and dozens of grammars" -- the same mechanisms at volume.

### What it mirrors today (buildable on the current ruleset)

| tOyCaml component | Engine shape it stands in for | Ruleset feature exercised |
| --- | --- | --- |
| `tc_ast.ml/.mli` | a generic AST node type | multi-module library + `.mli` |
| `tc_pattern.ml/.mli` | "a pattern is code with metavariables" | intra-library dep |
| `tc_lexer.ml/.mli` | the lexing stage | the fetched-from-source `re` opam dep |
| `tc_parse.ml/.mli` | the parsing stage | inter-module ordering (`ocamldep -sort`) |
| `tc_matcher.ml/.mli` | the matching engine core | inter-library dep on `:toycaml_intern` |
| `tc_intern.ml/.mli` + `intern_stubs.c` | the lone first-party C foreign-stub | `c_srcs` |
| `main.ml` | the CLI entry point | `ocaml_binary` + `build_test` |
| `matcher_test.ml` | engine tests | `ocaml_test` |

### What it must grow to cover (the gap, all public)

Each item is a load-bearing build feature of the public engine, with where it is
observable in `github.com/semgrep/semgrep`. None is in the current ruleset; each
becomes a tOyCaml component as it lands.

| Feature | Where it shows in the public engine | tOyCaml component to add |
| --- | --- | --- |
| **flambda compiler + `-O3`** | the root `dune` sets `(ocamlopt_flags (:standard -O3))`; `semgrep.opam.locked` pins `ocaml-option-flambda` | build the whole toy with a flambda toolchain; a test asserts `ocamlopt -config` reports flambda |
| **codegen tool as a build action (`atdgen`)** | many `(rule (action (run atdgen ...)))`, including the tree-sitter OCaml runtime bindings | a small `.atd` generates a result type the matcher emits |
| **`visitors`-style ppx + wider ppx set** | `visitors`, `ppx_deriving`, `ppx_sexp_conv`, `ppx_hash`, ... in the dune tree | derive a traversal over the AST instead of hand-writing it |
| **system-library vendoring** | `conf-gmp`, `conf-libpcre`, `conf-libev`, `conf-zstd`, ... in the lockfile | a `pcre` regex rule; a `gmp`-backed bignum |
| **non-dune opam package escape hatch** | `zarith` (hand-rolled configure + make, not dune) is in the closure | build `zarith` via an override |
| **static final link** | manylinux/musl-static wheels; the link flags are computed per platform | statically link the `toycaml` binary |
| **submodule-fetched grammars** | ~30 tree-sitter grammars are git submodules | fetch one grammar + its `parser.c` and bind it |

Sequencing note this surfaces: `atdgen` is not an edge case -- it generates the
tree-sitter OCaml runtime bindings, so it must work *before* the grammar/C-stub
work, not after it. ADR 004's plan ordered it the other way; tOyCaml makes the
dependency explicit.

### JS/WASM is out of scope

The engine's native path (`ocamlopt`) is the target. The public playground ships
a `js_of_ocaml`/wasm bundle, but that is a separate compilation backend
(bytecode via `ocamlc`, then `js_of_ocaml`/`wasm_of_ocaml`) that the native-only
driver cannot produce. We explicitly do not pursue it here; if it is ever needed
it is a new ADR, not a tOyCaml component.

## Alternatives Considered

- **Keep the generic examples.** Rejected: they do not exercise the real feature
  set and hide ordering constraints (the `atdgen`-before-grammars inversion
  above only became obvious by mapping a representative target).
- **Go straight to the real engine (Phase 8 first).** Rejected: too large to land
  green incrementally, and it forces the public/private and submodule/volume
  problems before the mechanisms are proven on something small.
- **Vendor the public engine into this repo as the example.** Rejected: heavy,
  slow, and unnecessary -- mirroring the shape proves the mechanisms; volume is
  Phase 8's job.

## Security

Baseline per `docs/security.md`. tOyCaml is original first-party code plus the
already-pinned `re` dependency; it introduces no new fetched artifacts and no
product source.

## Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| The toy drifts from the real engine's shape over time | Low | Low | This ADR's mapping table is the contract; revisit when Phase 8 starts |
| A feature is "demonstrated" on the toy but breaks at engine volume | Medium | Medium | The toy proves mechanism, not scale; Phase 8 keeps its own re-plan checkpoint |

## References

| Resource | Relevance |
| --- | --- |
| `bazel/ocaml/examples/toycaml/README.md` | the component-to-feature map, runnable |
| `docs/decisions/tooling/004-ocaml-rules-for-semgrep.md` | the scaling decision this serves |
| OCaml rules/semgrep scale plan | the phase plan tOyCaml is the acceptance target for |
| `github.com/semgrep/semgrep` | the public engine whose build shape is mirrored |
| `docs/decisions/tooling/006-extensible-multiarch-ocaml-toolchains.md` | how the toy builds per-arch |
| `docs/decisions/tooling/007-ocaml-build-file-generation-gazelle.md` | how the toy's BUILDs are generated at scale |
