Problem. Before this change, `humanGate` in
`projects/factory/reviewer/review/gate.go` gated every changed path whose base
name is `INVARIANTS.yaml`, and every path an invariant names as a mechanism
(`mechanisms.covers`). This diff removes both conditions. `humanGate` now
gates a path only when it cannot be looked up, matches the binding's
`HumanGatePaths` (REVIEW.md "Joe decides"), or is an invariant file that
`invariantPolicy.weakened` reports. `engine.go` passes that list at both call
sites, so a coder's new invariant no longer widens the gate.

1. An added invariant or a mechanism-file edit gets no `human-gate` finding,
   and a removed or weakened invariant still does:
   - `TestAddingInvariantGetsNormalReview` (engine): a PR that adds `X-2`
     and edits its mechanism file `projects/example/a.go` completes with
     conclusion success and no gate.
   - `TestWeakeningInvariantFileRequiresApprovalOnHead` (engine): a PR that
     removes invariant `X-1` is held until an approval of that head.
   - `TestInvariantGateOnlyForWeakening` (unit): no gate for a mechanism-file
     edit, an added invariant, an `unproven` to `live` move or a new file.
     Gated cases: a removed invariant, a changed statement, a dropped test, a
     deleted file, a renamed file, an unparsable head, a duplicate id, and a
     symlink that the Contents API resolves to identical text
     (`regularFileDiff` compares line counts with the diff).
   - `TestInvariantHeadReadErrorFailsClosed`: a failed head read is an
     error, so the check holds.
2. The count of gated open factory PRs can only be checked after rollout.
   The Activation trailer of the head commit names that check.
