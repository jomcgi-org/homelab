# Runbook: EmberVM stateful volume generation quarantine

## Symptom

A stateful workload (e.g. `demo-postgres`) will not wake. The control plane logs, on
a ~10s loop:

```
embervm stateful wake refused: volume quarantined (unblessed generation)   (StatefulManager.plan_wake)
embervm stateful wake failed  reason=:volume_quarantined                   (StatefulManager.finish_wake_failure)
```

preceded once by:

```
embervm stateful volume quarantined: unblessed generation reported past the last blessed one
  event=:generation_quarantined workload=<wl> generation=<G+k> blessed_generation=<G>
```

Downstream, the public health aggregate goes unhealthy and `jomcgi.dev/health` returns
503 with `{"status":"unhealthy","backendStatus":503}` (the `demo_postgres` component
reports `evicted (pair_broken) for <N>s, wake path likely broken`).

## What it means

The R7 blessing model (ADR embervm/011, standing decision 4) makes the control plane the
sole issuer of a volume's generation. A node reporting a generation PAST the last
CP-blessed one with `generation_blessed:false` on the wire is treated as a possible
split-brain write and the volume is QUARANTINED. `plan_wake` then fail-closes on every
wake (no auto-heal by design). See the "generation blessing and quarantine" moduledoc in
`projects/embervm/control/lib/embervm/stateful_store.ex`.

## Cause 1 (fixed): checkpoint-abort self-bump

Before the abort-blessing fix
(`docs/plans/2026-07-22-embervm-abort-generation-blessing.md`), the ADR-008
interruptible-bank ABORT path self-bumped the volume generation
(`volume.Manager.BumpGeneration`) without the CP blessing it, so a normal abort (a parked
connection resuming the VM hot) left the volume unblessed and quarantined it forever. The
fix has the CP bless the abort generation before dispatch and noded `RecordBlessed` it, so
a CP-driven abort never quarantines. If you see this on a build that predates the fix, the
recovery below clears it.

## Cause 2 (residual, accepted): node self-abort with no CP reachable

`autoAbortCheckpoint` (noded's resolve-timeout backstop) resumes a paused VM when the
control plane did not resolve the checkpoint in time. With no CP reachable to issue a
generation, it self-bumps (`blessed_generation` 0). The next report can quarantine the
volume. This is CORRECT fail-closed behaviour: the CP genuinely never witnessed that
resume, so refusing to wake until a human blesses it is the safe default. It is rare (the
CP pre-blesses before the timeout in the common case) and always accompanied by the
`:generation_quarantined` warning above.

## Recovery (break-glass): bless the reported generation forward

Blessing advances the CP's `blessed_generation` watermark to the generation the node is
already at, which clears the quarantine (`update_quarantine` only fires when
`reported_gen > blessed_gen`). This is safe: the node's on-disk generation IS the
authoritative latest state; you are recording reality, not overwriting it.

1. Read the quarantine warning to get `workload` and the reported `generation` (the node's
   current generation, `G+k` in the log).
2. From an `iex` remote console on the control-plane pod
   (`kubectl exec -n embervm <control-plane-pod> -c embervm -- ...` / `bin/embervm remote`),
   bless that exact generation:
   ```elixir
   Embervm.StatefulStore.bless_generation(Embervm.StatefulStore, "<workload>", <reported_generation>)
   ```
   The monotonicity guard refuses a value at or below the current watermark, so pass the
   node's reported generation (strictly greater than the stale blessed value).
3. The next wake proceeds (`plan_wake` no longer refuses). Confirm with
   `curl -s https://jomcgi.dev/health` returning 200 and the `demo_postgres` component OK.

If the wake still fails after blessing, the volume/snapshot pair is genuinely broken (not
just unblessed) and this runbook does not apply: investigate `pair_valid?` and the banked
bundle's `snapshot_generation` vs the volume generation.
