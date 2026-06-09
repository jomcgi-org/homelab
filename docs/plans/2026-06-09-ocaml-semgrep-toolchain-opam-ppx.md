# OCaml ruleset: Semgrep-aligned toolchain, opam resolution, ppx

Status: in progress
Owner: Joe
Branch: `claude/dazzling-brahmagupta-rv7npg` (PR #2468 carries the precursor work)

## Goal

Grow `bazel/ocaml` from a toy into a ruleset that can build a real dune-based
OCaml project — concretely, the path toward building **Semgrep CE** with Bazel.
That requires three things the toy lacks: a compiler that matches Semgrep, real
(transitive, non-stdlib) opam dependency resolution, and ppx preprocessing.

## Where we are (done on PR #2468)

- Thin native rules (`ocaml_library`/`ocaml_binary`/`ocaml_test`) over a hermetic
  compiler staged as action inputs; whole-library compile via `ocamldep -sort`.
- **opam-from-dune**: fetch a pinned package tarball and generate its BUILD by
  translating its own `dune` file (`opam/dune2bazel.py`). Demonstrated with
  `re` 1.11.0.
- **C stubs**: `c_srcs` on `ocaml_library` — `.c` compiled by `ocamlopt`, folded
  into the library `.a` (`examples/c_stubs`).

## Key facts that set the design

- **Semgrep CE pins OCaml `>= 5.3.0`**, specifically the custom variant
  `git+https://github.com/semgrep/ocaml.git` branch `5.3.0-semgrep`
  (commit `8521d35`). That branch is **stock 5.3.0 + a thin patch set** (notably
  "preserve `backtrace_enabled` between domains"). Building it ≈ building stock
  5.3.0.
- We are currently on **Debian bullseye OCaml 4.11.1**, which has bitten us
  repeatedly (re ≥1.12 needs 4.12's `List.equal`; parsexp/modern packages need
  4.14). The compiler version gates which package versions build and how ppx
  works, so the bump must come first.
- The Debian debs are **stripped of `compiler-libs`** (no `ast_mapper`/
  `ocamlcommon.cmxa`), which blocked ppx. A **from-source build installs
  `compiler-libs`**, unblocking ppx.
- The hermetic-sourcing trick (old-glibc bullseye binaries running
  forward-compatibly on the RBE executor, ubuntu-24.04 / glibc 2.39) does **not**
  transfer to 5.3: no old-glibc distro ships it. A 5.3 compiler must be sourced
  another way (build from source, or a relocatable/musl-static build).

## Spike results (validated locally on this environment)

Building `semgrep/ocaml@8521d35` from source + a ppx smoke test confirmed the
whole Phase-0 premise before any rule wiring:

- `./configure && make -j && make install` **succeeds**; `ocamlopt.opt -version`
  reports `5.3.0+semgrep-fork@8521d35...` (we get the exact fork).
- `lib/ocaml/compiler-libs/{ocamlcommon.cmxa,ast_mapper.cmi}` **are installed** —
  the ppx blocker is gone.
- A `compiler-libs` `Ast_mapper` rewriter builds and, via `ocamlopt -ppx`,
  rewrites `[%hello]` in a consumer (prints `hello from 5.3 ppx`). ppx is proven
  on 5.3.

## Decisions taken

- **Target Semgrep's exact fork** (`5.3.0-semgrep` @ `8521d35`), not stock — same
  build cost, faithful to the goal.
- **Bump the toolchain first** (Phase 0), before opam-resolution/ppx.
- Keep the existing whole-library compile model for now; per-module/Gazelle is a
  later generalization (Python `dune2bazel` is right for fetch-time generation of
  simple deps; a Go Gazelle extension driven by `dune describe` is the
  destination for monorepo scale — see PR #2468 thread).

## Open sub-decision (Phase 0 sourcing)

How the 5.3 compiler is materialized for actions:

- **A — build from source in the repository rule** (`./configure && make`).
  Simplest, fully hermetic, installs `compiler-libs`. Cost: ~minutes per *clean*
  fetch; risk that BuildBuddy CI runners don't persist the repo cache, paying it
  every run.
- **B — build once, pin a relocatable prebuilt tarball** (URL + sha256, like
  `debs.bzl`). Fast fetches, matches the existing pattern. Cost: a one-time
  build+host pipeline and glibc/musl portability handling.

Recommendation: **A first** to validate the 5.x migration end to end, then **B**
to keep CI fast once the shape is proven.

## Phases

### Phase 0 — toolchain bump to OCaml 5.3.0-semgrep

1. Replace the bullseye-deb sysroot with a 5.3.0 build (sourcing A→B above):
   new `toolchain/` fetch/build, updated `repositories.bzl` module extension.
2. Revalidate the driver on 5.x: `OCAMLLIB` relocation, host `as/gcc/ld` linking,
   the C-stub `ar` fold. OCaml 5 has the multicore runtime — confirm native
   compile + link still work unchanged.
3. Update `re` to a modern version (≥1.12, now that `List.equal` is available);
   drop the "1.11.0 ceiling" note.
4. Confirm `compiler-libs` present (done in spike) — it gates Phase 2.
5. Exit criteria: `bazel test //bazel/ocaml/...` green on 5.3; all existing
   examples (hello, regex, c_stubs) build on the new compiler.

### Phase 1 — transitive opam dependency resolution

1. Extend `dune2bazel.py`: resolve `(libraries X)` where `X` is **another fetched
   opam package** to `@ocaml_X//:X` (beyond stdlib/opam_deps). Handle the no-op
   `(preprocess no_preprocessing)` and dev-only `(lint …)` fields.
2. Pin a small transitive chain (two pure-OCaml dune packages, A depends on B),
   validated on 5.3. Candidate selection at implementation time — the 4.11
   ceiling that disqualified earlier candidates (parsexp needs `base.caml` +
   codegen; angstrom/bigstringaf need a configurator) is lifted on 5.3. Prefer a
   chain with no `(rule)` codegen / configurator first.
3. Exit criteria: a fetched package builds against another fetched package,
   transitively, from dune metadata.

### Phase 2 — ppx preprocessing

1. Add a `ppx` attr: a ppx rewriter is an `ocaml_binary` built against
   `compiler-libs` (present); the driver links it and passes `-ppx <exe>` to
   compiles. Mechanism already proven in the spike.
2. Demonstrate with a `compiler-libs`-only rewriter first, then wire `ppxlib`
   from source via Phase-1 resolution to reach real `ppx_*` packages.
3. Note: OCaml 5.x changed `Ast_mapper.register`/AST shapes (`Pconst_string`,
   one-arg mapper) — rewriters target the 5.3 API.
4. Exit criteria: a library with `(preprocess (pps …))` builds through our rules.

### Goal — build a real dune subtree

With 5.3 + transitive resolution + ppx + C stubs, build a non-trivial real dune
package (stepping stone toward Semgrep's own tree), and reassess the Go/Gazelle
migration for monorepo scale.

## Risks / watchpoints

- **CI fetch time** if Phase 0 stays on source-build (Option A) — drives the move
  to B.
- **OCaml 5.x driver surprises** — multicore runtime, AST changes for ppx.
- **Codegen/configurator dune stanzas** (`(rule)`, `dune-configurator`) remain out
  of scope; the generator keeps rejecting them loudly until explicitly handled.
- Each phase is validated locally against the exact CI compiler before pushing,
  then confirmed on BuildBuddy CI (no local `bazel test`).
