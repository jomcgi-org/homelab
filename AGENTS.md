# AGENTS.md

Build and run this homelab autonomously against Joe's goals, including
improving the factory itself. Choose simple reversible approaches, resolve
routine design choices and repair in-scope failures without asking permission.

The factory home cluster (`projects/factory-cluster/`) runs the factory and
its platform. The GKE hub (`homelab-hub`) runs the older workloads, which move
to the factory home cluster (HOME-128). Services live under
`projects/<name>/`; Bazel builds them and ArgoCD deploys their Helm charts.

## Merge authority

An explicit request, accepted issue/spec, established goal or standing
maintenance policy authorizes implementation, review, merge and rollout.
Routine human review or a separate "merge" instruction is unnecessary.
Existing approval covers the same action and scope.

For gated changes, factory work authored by `jomcgi-factory[bot]` still needs
Joe's APPROVED review of the head. A session Joe guides on his `jomcgi`
account may satisfy the human gate through the authorship waiver in
`REVIEW.md` "Joe decides", and lands a gated change only when Joe asked for
it in that session
([decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6891640540),
[#8049 scope](https://github.com/jomcgi-org/homelab/issues/8049)).
Renovate is excluded even on Joe's account. Explicit holds still require an
APPROVED review; required CI, independent review and the merge queue apply.

Routine autonomous maintenance includes model choice and IDs, and supported
CLI/runtime version bumps. Use proportionate compatibility checks, including
the service's dev conformance gate where applicable. Preserve the review, CI,
merge queue and verified rollout evidence below. Diagnose failed gates and
repair or roll back within scope. A model or version change alone needs no new
human decision.

The public `jomcgi-org/homelab` repository is the authorized publication
destination for reviewed homelab model/runtime changes and their accompanying
infrastructure documentation. Joe confirmed this standing permission on
2026-11-07.

Escalate only conflicting goals, a human-owned constraint exception, unapproved
destructive work, or an explicit human-owned decision/hold. Name the decision
and the rule requiring it. Keep independent work moving. If Joe may be away,
the delivery owner sends one `monolith-monolith-agent-notify` message when
available. Factory workers report to their dispatcher.

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

itsaplan project HOME tracks outstanding work; commits and PRs say
`Refs HOME-N`
([decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6892196446)).
A session on Joe's account may record his decisions, from an account that is
his alone (`docs/agents/reference.md` "Tracker"); domain `ARCHITECTURE.md`
files record decisions and link that record. New work goes to HOME; open
GitHub issues stay valid until closed or moved there, and GitHub Issues
otherwise take outside reports. Read failure evidence before retrying and
confirm inherited claims against current code, checks or live state.

## Evolution

Continuous self-improvement and useful delivery velocity are the prime goal.
The factory owns its working methods and budgets, and may add, adjust or remove
them to advance Joe's goals. Factory execution has no standing dollar cap.
Agents may change prompts, tooling and these instructions through reviewed
delivery. Use the
[evolution contract](docs/agents/evolution.md) for measurable experiments and
durable lessons. Factory prompts choose the work and its sequence.

## Boundaries

- GitOps: cluster access is read-only; manifests change through PRs. While
  ArgoCD does not yet own the factory home cluster, Claude may write to it
  with a `KUBECONFIG=~/.kube/factory.yaml` prefix.
- Preserve required CI, independent review, merge queue, admission controls,
  ownership and task limits. Never bypass gates or clear explicit holds.
- CI owns chart version increases, deployment pin advances and image pushes;
  lowering a deployment pin remains the revert lever.
- Secrets use the 1Password Operator. Git declares External Secrets Operator
  and the `factory-credentials` namespace to mint short-lived credentials
  from them. On 2026-11-18 neither ran on the cluster (HOME-67). Internet
  traffic uses Cloudflare.
  Preserve authentication and public/private data boundaries.
  [Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-67#comment-425).
- `human` means human-owned; `needs-human` requests a decision. Explicit
  handoff is required to change human ownership; removing the label does not
  resume an admitted receipt.
- Use Conventional Commits and concise prose per `docs/writing.md`.

## Writing

Docs, PRs, commits, comments, tickets and chat replies follow about 80% of
ASD-STE100 Simplified Technical English (`docs/writing.md`). Draw a flow,
state machine or boundary as a diagram instead of paragraphs. Change a doc
by replacing text, and add text only for a new fact.

## Commands and gotchas

Use scoped `AGENTS.md` files and the [task-specific reference](docs/agents/reference.md)
when relevant. `ci` is the local validation entry point; PR CI covers guests
without it. Operational commands belong to the role that owns that action.
