# ADR 007: OCaml first-party BUILD generation via Gazelle

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-06-10
**Supersedes:** ADR 004 Open Question 3 and the "keep per-library, revisit later"
framing of first-party BUILD authoring

---

## Problem

ADR 004 left first-party BUILD-file management as an open question: *"At what
scale per-library actions stop being acceptable and a per-module (Gazelle-style)
generator pays for itself?"*, with the standing answer "keep hand-authored
targets, revisit later." That was the right call for a toy. It is the wrong call
now, for a reason that has become concrete as the ruleset approaches real
functionality:

The public engine (`github.com/semgrep/semgrep`) has **188 first-party
`(library)` stanzas in CE alone** (ADR 004's own inventory), most of them
multi-module, on top of a transitive opam closure in the hundreds. Hand-writing
and -- the real cost -- *keeping in sync* a BUILD file per library as the dune
tree changes is untenable and silently error-prone. `dune` is the source of
truth for that tree; humans editing a parallel set of BUILD files by hand is a
standing drift hazard.

The question "when does a generator pay for itself" now has an answer: **at this
scale, it is required.** Deferring further only accrues hand-written BUILDs that
become migration debt.

## Decision

Adopt **first-party BUILD-file generation as a committed workstream**, and
supersede ADR 004's deferral of it.

- A **Gazelle extension** (Go) consumes authoritative dune metadata --
  `dune describe` for the module/library/dependency graph, falling back to
  `ocamldep` -- and emits `ocaml_library` / `ocaml_binary` / `ocaml_test`
  targets. First-party BUILDs are **generated, never hand-maintained**.
- It **mirrors the in-repo precedent** `bazel/helm/gazelle` (`language.go`,
  `generate.go`, `config.go`): the repo already runs a Gazelle language
  extension in CI, so the framework, the `format` integration, and the Go
  tooling are all established.

### Scope: what this does and does not settle

- It settles **BUILD generation** -- who authors and maintains first-party BUILD
  files: the generator.
- It does **not** force per-module **action granularity**. ADR 004's choice to
  keep one compile action per library stands; the generator emits per-library
  targets initially and can move to per-module later as a generator option,
  gated on RBE cache-hit data. Generation and action granularity are decoupled.
- **Boundary with the existing generator.** `opam/dune2bazel.py` stays as it is:
  fetch-time generation for third-party opam packages (translate a fetched
  package's own dune file at repo-rule time). Gazelle is for the **in-tree,
  first-party** source. The two are complementary, split by where the source
  lives, not competing.

### Coupling to tOyCaml

tOyCaml's hand-written BUILDs (ADR 005) are the generator's **first acceptance
corpus**: the generator must reproduce them from the sources alone. That gives
the extension a small, green, representative fixture before it is pointed at the
real engine's tree.

### Timing

The *decision* is accepted now so that new first-party code is generator-shaped
from the start and we stop accruing hand-authored drift. The *extension itself*
is a later phase/PR: it post-dates the core feature work in the scaling plan and
the per-module-vs-per-library data that informs what it should emit. No
generator code lands in this change.

## Alternatives Considered

- **Keep hand-authored BUILDs (ADR 004 status quo).** Rejected: untenable at
  188+ libraries; drift against the dune tree is silent and constant.
- **A bespoke Python generator like `dune2bazel` for first-party too.** Rejected:
  `dune2bazel` is shaped for fetch-time, single-package translation, not for
  managing/updating a live monorepo tree. Gazelle's incremental fix/update model
  and directive system are exactly that job, and the repo already runs one.
- **Run `dune` inside one big Bazel action.** Rejected in ADR 004: forfeits
  remote caching and cross-graph parallelism, reduces Bazel to a cron around
  dune.
- **Defer again until cache-hit data exists.** Rejected: that data informs
  *action granularity*, not whether generation is needed; generation is needed
  regardless, and deferring grows migration debt.

## Security

Baseline per `docs/security.md`. The generator reads dune metadata and writes
BUILD files; it executes no fetched code. `dune describe` runs the pinned,
from-source toolchain already used by the ruleset.

## Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| `dune describe` output is harder to consume than expected | Medium | Medium | tOyCaml is the small first fixture; fall back to `ocamldep` for the graph |
| Generator scope creep (tries to model all of dune) | Medium | Medium | Generate only the stanzas the ruleset supports; reject-loudly on the rest, same contract as `dune2bazel` |
| Divergence from `bazel/helm/gazelle` conventions | Low | Low | Mirror its structure deliberately; it is the in-repo reference |

## References

| Resource | Relevance |
| --- | --- |
| `docs/decisions/tooling/004-ocaml-rules-for-semgrep.md` | the decision this supersedes in part |
| `bazel/helm/gazelle/` | the in-repo Gazelle extension this mirrors |
| `bazel/ocaml/examples/toycaml/` | the generator's first acceptance corpus (ADR 005) |
| `bazel/ocaml/opam/dune2bazel.py` | the complementary fetch-time third-party generator |
| Dune `describe` docs | the authoritative first-party module/lib/dep graph source |
