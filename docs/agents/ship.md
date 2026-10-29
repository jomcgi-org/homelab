# Ship a feature

Deliver an authorized feature through merge and, when applicable, verified
rollout under `AGENTS.md` "Merge authority". Joe's local Claude Code command
is `/ship "<feature>"`; this repository does not install it.

This guide is for an end-to-end delivery owner. Factory workers follow their
assigned role and result contract; factory prompts schedule the work.

## Work to completion

- Read the request and existing issue. Infer routine details from the repo,
  choose a reversible approach and proceed. Ask only when the escalation
  conditions in `AGENTS.md` apply.
- Use an existing issue for multi-step work, or create one with the outcome,
  acceptance criteria and remaining work. A small explicit request can use
  its PR as the record. Do not create a second planning ledger.
- Keep one PR when the change is coherent. Split independently deliverable
  work when that improves throughput. Delegate independent implementation
  tasks when useful; give each agent its scope, acceptance and constraints.
- Implement and validate the behaviour in the same change. Meet the root
  delivery outcomes with proportionate checks, independent review, required
  CI, merge queue and live verification where needed. Choose the sequence
  appropriate to the task.
- Fix in-scope failures yourself. Read failure evidence before retrying.
  Escalate a specific unresolved decision once; continue unrelated work.
- Close the issue when its acceptance criteria are met. If rollout or
  observation is outstanding, leave it open and record the exact remainder.
  Report the PR URL and actual state.

## Add only what the change needs

- **Architecture:** update the domain's `ARCHITECTURE.md` when adding a
  service/integration, changing a data model or cross-service contract,
  moving a trust boundary, or reversing a decision. Record the rationale
  in a `**Why.**` paragraph; link unbuilt work from Direction to its issue.
- **Tests:** cover changed behaviour and failure paths. BDD is useful for
  user-visible flows; a separate specification PR is optional. Use
  `bdd_test(...)` with `@covers_*` markers from `shared/testing/markers.py`.
  Demonstrate the failure on the implementation branch, then make the specs
  pass before merging. Ordinary BDD targets run in required `pr-checks`;
  the unused future lane was retired in #7088.
- **Safety model:** if the system has `STPA.md`, update it using
  `docs/agents/stpa.md` when changing control actions, feedback/freshness,
  RBAC, deletion/retention or autonomous actions. Additive UI, copy, docs
  and changes outside a control loop need no STPA pass.

Keep related records and tests in the implementation PR where practical.
There is no mandatory phase sequence, separate PR per phase, or checklist
of skipped phases.
