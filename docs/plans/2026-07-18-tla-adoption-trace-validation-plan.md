# Plan: TLA+ adoption spec, layer-2 trace validation (PR2)

**Status:** Deferred (parked follow-up). Do not start until the resume trigger below fires.
**Created:** 2026-07-18
**Owner:** Joe McGinley
**ADR:** [embervm/006](../decisions/embervm/006-tla-formal-specification-pilot.md)
**Predecessor:** PR1 (merged as #3697) shipped the adoption spec, the TLC toolchain, and the layer-1 vocabulary guard. See `projects/embervm/specs/README.md`.

## Why this is parked

PR1 delivered the pilot's foundational half: `adoption.tla` runs in CI over four TLC configs (safety, liveness, and two negative modes that must keep re-finding the historical wedge and resurrection bugs), and the layer-1 vocabulary guard fails the build when a new proto verb, health state, or op-log kind lands unclassified. That machinery earns its keep on every commit regardless of whether PR2 ever ships.

PR2 (layer-2 trace validation) is the more valuable but higher-maintenance half: it ties the model to the *running* system by mapping real op-log events onto TLA+ actions and running TLC in trace-checking mode, so a divergence between spec and code fails CI instead of rotting silently. The ADR is explicit that the event-to-action translator is real work "comparable to the spec itself."

The reason to wait: trace validation is only worth building against a *stable* protocol surface. EmberVM is still churning through the distribution rungs (R6 continuity, R7 distribution in progress per ADRs 009/011, plus the component rename in #3694 and artifact-decoupling in #3704). The adoption protocol itself (dispatcher inventory, node-registry health machine, op-log task-state verbs) is stable, but the op-log schema and the e2e drill harness that would source a trace are both still moving. Building the translator now means chasing those changes; building it after the surface settles is a one-time cost. This mirrors the ADR's own timing logic for deferring the spec work in the first place: specifying (or trace-checking) a churning protocol multiplies the drift problem the pilot exists to contain.

## Resume trigger

Start PR2 when **both** hold:

1. The adoption-relevant surfaces are stable: no open ADR or in-flight PR is changing `op_log.ex` verbs, the `NodeRegistry` health machine, `Dispatcher` inventory/adoption, or the e2e drill harness that produces a trace. Practically: R6/R7 distribution work has landed and the component rename (#3694) alias rollout is complete.
2. PR1's four TLC configs have run green against real commit traffic for long enough to trust the safety config's state count and the two negative modes are stably reproducing their bugs (a short soak, not a fixed date).

If, by the time both hold, the ADR's exit judgment has already been reached by other means (a real interleaving bug surfaced against the adoption spec, or the layer-1 guard alone has proven sufficient and the appetite for more conformance plumbing is gone), reconsider whether PR2 is still wanted before building it. The pilot is explicitly allowed to stop at "keep the one spec as documentation."

## Goal

One CI target that maps op-log events from an embervm e2e drill to `adoption.tla` actions and runs TLC in trace-checking mode, failing the build if the drill drove the implementation through a step the model forbids. Delivering this satisfies the ADR's layer-2 success criterion ("a drill trace checked against the adoption spec") and, together with any bug or divergence found, feeds the ADR's exit judgment on whether to extend to protocols 2 (session bank/relight) and 3 (quota gate) and build layer 3.

## Design decisions to settle first (the ADR's open questions, now PR2's)

These are called out in ADR 006 as open questions. They are the first design fork of PR2 and should be resolved (simplest-approach-first, per repo philosophy) before any code:

1. **Trace source: op-log SQLite projection vs OTEL span export.** The op-log is durable, already ordered, and its verb list is exactly what layer 1 asserts on, so it is the obvious source and keeps the translator table-driven off a surface the vocabulary guard already polices. The catch the ADR names: ETS-only transient states (`:banking` / `:relighting`) are never op-logged, so an op-log-only trace is blind to them. For the *adoption* spec specifically this is a non-issue (adoption models durable task-state and the health machine, not the session ETS states), so **recommend: op-log projection as the trace source for PR2**, and revisit OTEL spans only if/when protocol 2 (sessions) is specced and needs the transient-state edges. State this explicitly rather than leaving it implicit.
2. **Where TLC trace-checking runs.** PR1 established the `//bazel/tla` prebuilt toolchain (pinned `tla2tools.jar` v1.7.4 + pinned Temurin JRE, driven by `tlc.sh`). Trace checking is linear in trace length (unlike the exhaustive safety config), so it is cheap enough to run per-push. Reuse the same toolchain and driver; add a trace-checking mode to `tlc.sh` rather than a new tool image.
3. **Translator ownership.** Keep the event-to-action mapping table-driven off the op-log verb list that layer 1 already classifies in `vocabulary.exs`, so one mapping serves both guards and adding an op-log verb forces a decision in one place. One translator, shared across specs when protocols 2/3 arrive.

## Build sequence (once unparked)

Ordered, each step independently reviewable. Implement, commit, push, watch CI (no local Bazel test loop).

1. **Trace-checking mode in the TLC driver.** Extend `//bazel/tla:tlc.sh` to accept a trace file and run TLC in trace-validation mode against `adoption.tla` (TLC's `-simulate` / trace-spec pattern, or an explicit `TraceSpec` module that constrains behavior to the recorded trace). Prove it out with a hand-written toy trace before wiring real events. This is the load-bearing mechanism; get it green in isolation first.
2. **Op-log to TLA+ action translator.** A small module (Elixir, colocated with the op-log, or a spec-side script) that reads the op-log projection from a drill run and emits a TLA+-readable trace (sequence of action records: action name plus the state fields the model tracks). Table-driven off the op-log verb list; every verb either maps to a modeled action or is explicitly marked untranslated, reusing the `vocabulary.exs` classification so the two guards cannot disagree.
3. **Drill trace capture.** Identify (or add) an e2e drill that exercises adoption end to end (prime, assign, a control-plane restart with adoption reconcile, a node-down reassign). Capture its op-log as a CI artifact. The R6 gate drills already exercise this path (see the r6-gate-drills memory); prefer wiring into an existing drill over authoring a new one.
4. **CI target.** A `bazel test` target that runs drill -> capture op-log -> translate -> TLC trace-check, failing on any forbidden step. Wire into the same `//projects/embervm/specs/...` test group so it runs with the existing spec checks.
5. **Negative self-test.** Mirror the layer-1 and negative-config discipline: a deliberately corrupted trace (an event reordered to violate `NoDoubleAssign` or `forget-before-kill`) that the trace-checker MUST reject, so a silently broken translator (one that maps everything to a no-op and always passes) fails the build. Without this, layer 2 can rot exactly like the specs it is meant to keep honest.
6. **Docs + DECISIONS.** Update `projects/embervm/specs/README.md` (the Scope section currently says layer 2 "is PR2 and is deliberately not built here"). Record the trace-source decision and any divergence found as a `D-TLA.PR-2.x` entry. If a real divergence is caught, that is the ADR exit-judgment signal to extend the pilot.

## Success criterion

A CI-green trace-validation target that (a) checks a real adoption drill trace against `adoption.tla`, and (b) rejects a corrupted trace via the negative self-test. On completion, record the exit-judgment outcome per ADR 006: extend to protocols 2/3 and build layer 3 if a real bug or divergence was found, or stop and keep the spec as documentation if the plumbing proved to be ceremony.

## Non-goals

- Protocols 2 (session bank/relight) and 3 (quota gate). Contingent on the exit judgment, not part of PR2.
- Layer 3 (the scheduled `tla-conformance` review skill). Built only if the pilot survives its exit judgment.
- OTEL-span-sourced traces. Only needed once a spec models the ETS-only transient session states, which is protocol 2, not PR2.
- Any change to the running system. Specs and conformance checks are read-only repo/CI artifacts.
