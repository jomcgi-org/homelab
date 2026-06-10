# ADR 006: Extensible multi-arch OCaml toolchains

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-06-10
**Refines:** ADR 004 (fixes the mechanism for its per-arch decision)

---

## Problem

ADR 004 decided that multi-arch means **per-arch native executors, not
cross-compilation** -- one sysroot per arch, built on that arch's pool, selected
by Bazel constraints -- and named linux x86_64 + aarch64 as the near-term set.
What it did not fix is the *mechanism*. Two requirements shape it:

1. **Extensible.** Adding an arch (the two now, and plausibly a production arch
   later if this ruleset is used beyond the homelab) should be a one-line edit,
   not a new hand-written toolchain definition each time.
2. **Non-destabilizing.** There is no verified arm64 executor pool yet, and this
   repo has no local build loop -- correctness is observed only on CI. Wiring a
   second toolchain blind risks breaking the working x86_64 build. ADR 004 itself
   flags that the exact BuildBuddy execution-property names must be verified at
   implementation time.

## Decision

Make a **data-driven arch registry the single source of truth**, and **defer
live per-arch toolchain registration until a pool is verified**.

- `bazel/ocaml/toolchain/arches.bzl` defines `OCAML_ARCHES`: one struct per arch
  (`name`, `os`/`cpu` constraints, `bb_arch` BuildBuddy routing property,
  `enabled`). Adding an arch is appending one struct.
- A macro declares one `platform` target per arch under
  `//bazel/ocaml/platforms`. Platform targets are **inert** -- they affect
  nothing until a build selects them via `--platforms` or a toolchain's
  `exec_compatible_with`/`target_compatible_with` -- so declaring both arches now
  is free and gives stable labels to reference.
- Per-arch **toolchain registration** is gated by `enabled` and is the Phase-7
  work; it is **not wired in this change**. The live toolchain registration in
  `bazel/ocaml/BUILD` is unchanged (x86_64, exactly as today), so this change
  carries zero risk to the current build.
- Ship an **executor-arch probe** now (`//bazel/ocaml/platforms:executor_arch_probe`):
  a `genrule` that records `uname -m` from the default pool, wrapped in a
  `build_test`. It confirms what the executor actually reports -- the first step
  of verifying the arm64 pool and the exact `Arch` property key before any
  toolchain selects a platform.

This generalizes the existing "build the compiler on the executor it runs on"
design without changing it: because the compiler is an action, each arch builds
its own sysroot on its own pool by construction -- no cross-compilation, no
per-target compiler forks. It is the same shape the repo already uses for
dual-arch apko images.

## Alternatives Considered

- **OCaml cross-compilation.** Rejected in ADR 004 and again here: immature at
  5.3, per-target compiler forks dwarf the ruleset.
- **Hand-written per-arch toolchains.** Rejected: not extensible (duplication per
  arch), and the duplication is exactly what a production-arch addition later
  would have to repeat.
- **Wire arm64 toolchain registration now.** Rejected: no verified pool, no local
  build loop, and ADR 004 calls for verifying the property names empirically. The
  probe is the cheap first step instead.
- **Custom RBE executor image per arch.** Rejected per ADR 004: this BuildBuddy
  deployment does not honor per-action `container-image`.

## Security

Baseline per `docs/security.md`. No new fetched artifacts. The probe runs a
trivial `uname -m` on the existing executor.

## Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| The arm64 pool does not exist or behaves differently | Medium | Medium | Probe-first; `enabled=False` keeps it out of the live build until proven |
| The BuildBuddy `Arch` property key differs from the guess | Medium | Low | It is inert until a toolchain selects the platform; confirmed via the probe before Phase 7 wires it |
| Registry abstraction outlives its usefulness | Low | Low | It is a plain list + one macro; collapsible if it ever gets in the way |

## Open Questions

1. The exact BuildBuddy execution-property key(s) for arch routing (`Arch`, and
   any pool selector) -- resolved against executor docs + the probe at Phase 7.
2. Whether a production deployment would want additional arches beyond
   linux x86_64/aarch64 (the registry makes this a one-line answer when known).

## References

| Resource | Relevance |
| --- | --- |
| `docs/decisions/tooling/004-ocaml-rules-for-semgrep.md` | the per-arch-native decision this fixes the mechanism for |
| `bazel/ocaml/toolchain/arches.bzl` | the registry |
| `bazel/ocaml/platforms/BUILD` | generated platforms + the executor probe |
| `bazel/tools/platforms/BUILD` | the existing in-repo platform-declaration pattern |
