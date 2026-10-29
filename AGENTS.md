# AGENTS.md

Build and run this homelab autonomously. Maximise useful work shipped against
Joe's goals, including improvements to the factory itself.

The GKE hub (`homelab-hub`) is the only cluster. Services live in
`projects/<name>/` with their Helm `chart/` and GitOps `deploy/`.
Bazel builds the repo; ArgoCD deploys it. The factory chooses models.

## Merge authority

An explicit request, accepted issue/spec, established goal or standing
maintenance policy authorizes implementation, review, merge and rollout.
Do not wait for routine human review or a separate "merge" instruction.
Choose the simplest reversible approach within that authority and proceed.
Resolve ordinary design choices yourself; record material assumptions in the PR.

The factory owns eligible intake, refinement, implementation, review, landing
and recovery within its configured limits. Unblock failed checks, repair
regressions and improve its prompts, policies and tooling through this repo.
Prefer finishing existing work; parallelize independent work when useful.
A constrained worker hands off to an authorized dispatcher for actions outside
its allowlist.

Escalate only conflicting goals, an exception to a constraint or budget,
unapproved destructive work, or an explicit human ownership/approval hold.
Name the decision and the rule requiring it. Existing approval covers the
same action and scope. Keep independent work moving while one item is blocked.
Never bypass a gate, expand your own permissions or clear a hold to make progress.

## Delivery

1. Inspect the relevant code and scoped `AGENTS.md`. Use a dedicated branch
   and worktree, or a new branch through connected GitHub tools.
2. Implement the smallest complete change. Track outstanding work in GitHub
   Issues; use `agent-ready` when the task can be picked up alone.
3. Run `ci` before pushing when available; otherwise use PR CI. Add tests
   for changed behaviour and concrete failure cases. Docs and mechanical
   edits need proportionate checks, without invented test or planning phases.
4. Review the complete diff once with an independent agent
   ([review guide](docs/agents/review.md)), fix blocking findings and satisfy
   required checks on the current head. Record scope and validation in the PR.
5. Land with `homelab pr land <n>`, or `gh pr merge <n> --auto` and follow
   the merge queue. Pass no strategy flag. Rebase only for conflicts or to
   incorporate CI's branch writeback, never just because main moved.
6. For deployable changes, verify the rollout with `verify_deployment` until
   terminal. Without it, confirm ArgoCD Synced/Healthy, the new pod image and
   a responding service. Report the PR and actual result; docs need no rollout.

Read failed logs before retrying. Change the hypothesis when evidence disproves
it; do not loop on the same failed action. Verify another agent's completion
claims against the diff and checks. Never push to a merged branch.

## Boundaries

- GitOps only: cluster access is read-only. Change manifests in Git; no
  mutating `kubectl`, `helm install` or `helm uninstall`.
- Use PRs and the merge queue. Preserve branch protection, required CI,
  independent review, factory admission/ownership controls and worker limits.
- CI owns chart version increases, deployment pin advances and image pushes.
  Wait for chart writeback before diagnosing a rollout; lowering a pin is
  the revert lever.
- Keep secrets in the 1Password Operator. Internet traffic goes through
  Cloudflare. Preserve authentication and public/private data boundaries.
- `human` means human-owned; `needs-human` requests a decision. Do not
  compete with human-owned work or change `human` without explicit handoff.
  Removing it does not resume an admitted receipt; re-admission is required.
- Use Conventional Commits. Keep prose concise and follow `docs/writing.md`.

## Commands

`ci` runs changed-file lint, selective generation and affected remote tests.
Use it rather than bare Bazel. Never filter away its output; confirm tests
actually ran. Setup and generation details: [reference](docs/agents/reference.md#commands).

## Gotchas

Read [build and deployment gotchas](docs/agents/reference.md#gotchas) when
changing those paths. Scoped instructions live beside their code. Consult the
[task-specific index](docs/agents/reference.md#where-to-look-next) only as needed.
Use available KG/production evidence to shorten investigations, verify it
against current code and live state, and record durable findings.
