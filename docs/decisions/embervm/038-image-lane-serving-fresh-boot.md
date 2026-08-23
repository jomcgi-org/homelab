# ADR 038: Image-Lane Serving Fresh Boot

**Author:** Joe McGinley
**Status:** Draft
**Created:** 2026-08-23
**Relates to:** [ADR embervm/001](001-embervm-beam-firecracker-workload-orchestrator.md) (D-R3.11.2, which deferred this rung), [ADR embervm/018](018-node-local-activator-brick-authoritative-lifecycle.md) (the node-local activator whose cold-boot arm this unblocks)

---

## Problem

A serving VM fresh boot attaches two drives: the runtime rootfs and a
**cold-boot handler artifact**, the verified zip archive bytes persisted
during BuildBase so the guest can import its handler off disk before serving
(D-R3.11.2). The artifact exists only on the ZIP lane:

> "Only the zip lane carries an archive; an image-lane serving base has no
> handler zip and is left for a later rung."
> (`projects/embervm/noded/server/server.go:816-818`)

Both consumers of the serving-images inventory enforce it:
`startServingFresh` fails with "no cold-boot handler artifact built" when the
inventory entry is missing (`projects/embervm/noded/server/serving.go:91`),
and the node-local activator refuses with "no serving image provisioned"
(`projects/embervm/noded/server/activator.go:236`).

The 2026-08-23 live drill of the first serving-class workload (#5180) hit
exactly this rung: the prover guest uses `source.image`, its base built
cleanly (Ready=True, per-vendor refs exported), and the miss-wake still
failed at the inventory lookup. No chart workload has ever reached this path
because every shipped workload is task/session/stateful; Serving had no
consumer at all.

## Decision

Image-source serving bases join the serving-images inventory, and a fresh
boot boots them without a handler drive.

1. On a `BuildBase` with `serving: true` and NO archive (the image lane),
   noded registers the built rootfs itself as the inventory entry:
   `{base_key, workload, runtime_image_ref: image_digest, handler_path: "",
   size_bytes: 0}`. The control plane already stamps `serving:` from the
   workload class (`projects/embervm/control/lib/embervm/base_builder.ex:1977`),
   so this requires no control-plane or proto change.
2. `startServingFresh` attaches the handler-artifact drive ONLY when the
   selected entry has a `handler_path`. An image-lane entry boots the runtime
   image's own entrypoint (the `harness_init` resolved by image ref), which
   by contract serves HTTP on the tap-facing port directly.
3. Zip-lane behaviour is byte-for-byte unchanged: archive present means
   artifact written, drive attached, shim imports the handler.
4. The activator needs no code change beyond what falls out of 1: its
   inventory scan already selects the freshest entry whose runtime ref is
   provisioned (`projects/embervm/noded/server/activator.go:250-262`).
5. `NodeStatus.serving_image_ref` reporting iterates the same inventory
   (`projects/embervm/noded/server/server.go:2396`), so image-lane bases
   become wakeable by the control plane with no new fact.

## Alternatives Considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Reclassify consumers onto the zip lane | Would make every future web-API serving consumer bend around a gap: it becomes the first-ever zip chart workload too (archive hosting for `codeUri` is itself undecided), and the prover stops being language-neutral |
| Keep the deferral | Serving keeps zero possible consumers indefinitely; the class stays audit-UNTESTED forever |
| Wake image-source serving through the task lane | Wrong semantics: serving traffic rides the tap NIC with long-lived HTTP, not one-shot vsock invokes |

## Consequences

- Image-source serving works end to end: base build, CP miss-wake,
  node-local activator wake, idle bank, relight.
- The handler artifact stays MANDATORY on the zip lane; nothing about
  zip-lane builds, drives, or the shim's import step changes.
- Anything iterating the serving-images inventory must now tolerate an empty
  `handler_path` (image lane) alongside a populated one (zip lane); the only
  known consumers are the activator selector, `startServingFresh`, and the
  NodeStatus reporter, all covered by tests in the implementation PR.
- The guest contract for image-source serving is now explicit: the image's
  entrypoint IS the PID-1 HTTP server answering on the tap-facing port and
  the vsock readiness port.

## References

| Resource | Relevance |
| -------- | --------- |
| GitHub issue [#5180](https://github.com/jomcgi/homelab/issues/5180) | First serving-class workload; drill rounds 1 and 2 with evidence |
| `projects/embervm/noded/server/serving.go` | `startServingFresh`, where the artifact drive becomes conditional |
| `projects/embervm/noded/server/server.go` | BuildBase serving branch writing the artifact (zip lane) and the new image-lane registration |
| `docs/decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md` | D-R3.11.2, the deferral this ADR lifts |
