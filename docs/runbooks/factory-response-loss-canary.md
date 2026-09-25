---
name: factory-response-loss-canary
invoke: explicit
summary: Manually invoked bounded native canary for lost guest response receipt recovery (#5938)
---

> **Runbook (explicit-only).** Open only on explicit operator request. Never wired into `ci test`, never auto-scheduled, never granted a new always-on production credential.

# Factory response-loss canary (#5938)

This covers the residual operational acceptance for issue #5938. The repository contract is already implemented and live; this runbook only stages the manual canary and its checks. It changes no default and enables nothing by itself.

## What already exists (verify, do not re-add)

- Guest completion path: `projects/embervm/runtimes/claude/shim.py` validates `result_receipt` with `_valid_result_receipt`, publishes the native record with `_publish_result_receipt` before `self._send(200, record)`, which is the site of the observed `BrokenPipeError`.
- Control plane receipt and adoption: `projects/monolith/factory/execution/result_receipts.py`, `result_receipts_router.py`, `receipt_capture_test.py`, `receipt_consumer_test.py`, `response_lost_test.py`. Receipt writes and adoption are idempotent and reject conflicting, stale, and newer-attempt data.
- Live flags: `projects/monolith/deploy/values-gke.yaml` sets `resultReceiptsEnabled: true` (`AGENT_RESULT_RECEIPTS_ENABLED`), `resultReceiptAdoptionEnabled: true` (`AGENT_RESULT_RECEIPT_ADOPTION_ENABLED`) and `responseLostRecoveryEnabled: true` (`AGENT_RESPONSE_LOST_RECOVERY_ENABLED`). Capture, adoption by the active writer, and recovery are three separate controls, and the canary needs all three.

## Preconditions (all required)

1. An operator is present for the whole canary and can stop it.
2. Exactly one bounded factory or KG invocation is used. No batch, no loop, no schedule.
3. Capture, adoption and recovery are enabled on the hub: all three flags above read back true from the live monolith Deployment env (read-only `kubectl get deployment -n monolith -o jsonpath` filtered to `AGENT_RESULT_RECEIPT` and `AGENT_RESPONSE_LOST_RECOVERY_ENABLED`), not only from a render of git values, because Kargo owns what monolith runs.
4. No new credential is minted for the canary. Use only the operator's existing hub access.

## Bounded canary steps

1. Invoke one factory or KG turn against the GKE hub with capture plus adoption enabled.
2. Let the guest complete, then exercise the lost-response path: either observe a genuine transport loss or force loss at the synchronous response write only (never by killing the guest or replaying the model).
3. Confirm the receipt is captured for the exact admitted session, turn or dispatch attempt, guest, and request identity.
4. Confirm the active writer adopts the native record through the existing turn and artifact validation path, preserving interruption history, consumed bounds, provider cost, and extraction provenance.
5. Confirm zero duplicate model executions for the attempt and zero per-session database repair.
6. Stop after the single attempt regardless of outcome. A failure is evidence for the issue, never permission to retry with uncertain external effects or to widen the blast radius.

## Bound-but-not-invoked retained guests

The `not_invoked` and `interrupted_then_not_invoked` terminal-settlement path in `projects/monolith/factory/orchestration/factory_conductor.py` (around the `read_not_invoked_factory_attempt` proof) is a retained-guest disposition, not VM-cessation proof. Any eventual observation, retirement, or unbinding goes through the existing cleanup, fence, and identity contract only:

- `projects/monolith/factory/execution/guest_cleanup.py` (`sweep_once` plus `execution_api.reap_settled_session` with `store.settled_guest_cleanup_conditions`)
- The session fence and identity checks on that path

Never treat receipt availability or a retained-guest row as proof the external guest stopped. Replacement or capacity release requiring cessation still uses the authoritative contract.

## Live deploy confirmation

Monolith is one of the Kargo-owned charts where a git `targetRevision` alone does not prove what is live. Against the hub context, read the live ArgoCD object:

```bash
kubectl get application monolith -n argocd -o "jsonpath={.spec.sources[0].targetRevision}"
```

Then confirm the deployed image and flags actually carry the receipt and recovery code described above. If the render and the running pods disagree, follow `argocd-outofsync.md` before drawing any conclusion.

## Explicit non-goals

- This runbook is not part of `ci test` and must never be added to it.
- It is not scheduled and must never be auto-scheduled.
- It grants no credential and must never be given an always-on production credential.
- Operational acceptance stays on issue #5938. Do not close that issue from this runbook.
