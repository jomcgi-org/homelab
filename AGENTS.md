# AGENTS.md

Build and run this homelab autonomously against Joe's goals, including
improving the factory itself. Choose simple reversible approaches, resolve
routine design choices and repair in-scope failures without asking permission.

The GKE hub (`homelab-hub`) is the only cluster. Services live under
`projects/<name>/`; Bazel builds them and ArgoCD deploys their Helm charts.

## Merge authority

An explicit request, accepted issue/spec, established goal or standing
maintenance policy authorizes implementation, review, merge and rollout.
Routine human review or a separate "merge" instruction is unnecessary.
Existing approval covers the same action and scope.

Escalate only conflicting goals, a constraint or budget exception, unapproved
destructive work, or an explicit human-owned decision/hold. Name the decision
and the rule requiring it. Keep independent work moving.

## Factory roles

Factory prompts and pinned task contracts decide roles, models, action order,
tools and handoffs. A factory agent completes its assigned role and returns
the required artifact. The factory owns sequencing, retries, review and landing;
workers do not each start a second delivery workflow.

Repository authority applies through the owner of each action. An implementer
can deliver a PR while an authorized landing owner merges and verifies it.
Follow the assigned branch, scope and tool limits; broad autonomy never expands
a restricted worker's permissions. End-to-end delivery owners carry eligible
work through to completion without a routine handoff to Joe.

## Delivery outcomes

Claims need evidence: required checks and independent review must cover the
current PR head before landing through the merge queue. Changed behaviour
needs proportionate validation; docs need no invented test or planning phases.
Deployment claims need a verified live rollout. Role completion, merged code
and operational acceptance are distinct; report the actual state.

GitHub Issues track outstanding work. Domain `ARCHITECTURE.md` files record
decisions. Read failure evidence before retrying and confirm inherited claims
against current code, checks or live state.

## Boundaries

- GitOps: cluster access is read-only; manifests change through PRs.
- Preserve required CI, independent review, merge queue, admission controls,
  ownership and task limits. Never bypass gates or clear explicit holds.
- CI owns chart version increases, deployment pin advances and image pushes;
  lowering a deployment pin remains the revert lever.
- Secrets use the 1Password Operator; internet traffic uses Cloudflare.
  Preserve authentication and public/private data boundaries.
- `human` means human-owned; `needs-human` requests a decision. Explicit
  handoff is required to change human ownership; removing the label does not
  resume an admitted receipt.
- Use Conventional Commits and concise prose per `docs/writing.md`.

## Commands and gotchas

Use scoped `AGENTS.md` files and the [task-specific reference](docs/agents/reference.md)
when relevant. `ci` is the local validation entry point; PR CI covers guests
without it. Operational commands belong to the role that owns that action.
