# Collapse generated issue contracts

Refs #7452

The issue body displays the newest generated acceptance contract. Bounded
task-only markers enclose it, with the gate hash inside for audit. The
receipt's `direction_json["conductor_gates"]` retains every distinct decision.
Decision comments retain their separate per-task, per-hash retry markers.
Cleanup skips issues labeled `human` or `needs-human` and reports each hold.
Changing an issue body can supersede a pending decision card during escalation
revalidation because the card records the body's hash. Do not remove holds to
run cleanup. An authorized operational owner must resolve the decision lifecycle
before the issue can be cleaned up.

Historical writers appended contracts in body order, so cleanup retains the
last generated block, including when its task differs from earlier blocks.
Legacy recognition requires the exact generated rescope suffix followed by
contiguous single-line checklist items, unchecked or ticked (`[x]` or `[X]`).
Cleanup and identical gate retries preserve completion ticks. Blank-line-separated
human prose and manual lists remain byte-for-byte unchanged. Inline and fenced marker
examples are preserved. Malformed, nested, mismatched or unknown generated
marker content is an error. During gate resolution an ambiguous existing body
is left unchanged with an audit record; the persisted contract, decision card,
PR rescope and decision audit still proceed. Marker-free bodies are appended
directly, including when the human text contains an unfinished fence. Scope and
check text reject line breaks, leading whitespace and HTML comments before
persistence. Ambiguous legacy
content requires manual inspection; never extend a boundary to consume prose.
A manual checklist immediately adjoining an old generated checklist has no
recoverable boundary. Separate it using reviewed before/after evidence before
authorizing cleanup of that issue.

## Validate offline

From the repository root:

```sh
PYTHONPATH=projects/monolith python -m pytest projects/monolith/factory/orchestration/factory_gate_blocks_test.py -q
ci test -- //projects/monolith/factory/... //projects/monolith:swarm_factory_conductor_test //projects/monolith:swarm_factory_decisions_test //projects/monolith:swarm_factory_gate_blocks_test
```

The root Bazel targets explicitly reach the regression tests. Inspect the
`Executed N out of M tests` line. An exit status alone is insufficient.
An optional `--fixture issues.json` accepts an offline JSON array of REST issue
objects (`number`, `state`, `body`, `updated_at`, `labels`, optional `pull_request`). It
cannot be combined with `--apply`.

## Separately authorized live run

The implementation task does not authorize this run. The landing owner first
records verified deployment evidence tied to the delivered revision. An
operational owner then reviews the dry-run and authorizes application. The
command uses the existing `gh` account and is fixed to `jomcgi-org/homelab`.
It only enumerates open issues, follows every page and excludes PR objects.
No scheduled execution is installed.

```sh
PYTHONPATH=projects/monolith python -m factory.orchestration.factory_gate_cleanup > gate-dry-run.jsonl
# After separate operational authorization:
PYTHONPATH=projects/monolith python -m factory.orchestration.factory_gate_cleanup --apply > gate-apply.jsonl
PYTHONPATH=projects/monolith python -m factory.orchestration.factory_gate_cleanup > gate-repeat.jsonl
```

Retain these files privately as audit evidence. Change records contain complete
before/after bodies and SHA-256 digests. Application rereads each candidate
immediately before PATCH and refuses human holds, changed bodies, changed
`updated_at`, PRs and closed issues. This uses the repository's fresh-observation
pattern; the GitHub issue API provides no transaction spanning the read and write. Arrange
an operational quiet window and inspect the recorded response. Each write
intent is flushed before PATCH. A write failure or uncertain response stops
the invocation; inspect GitHub against that intent before another run. There
is no automatic write retry. Closed issues are never selected for cleanup.
Stale and ambiguous cases are reported and prevent a passing result.

## Complete audit

Run under a shell with failure propagation. Do not treat partial output from a
failed API command as complete coverage:

```sh
set -eu
gh api --paginate --slurp 'repos/jomcgi-org/homelab/issues?state=open&per_page=100' > gate-audit-pages.json
jq 'add' gate-audit-pages.json > gate-audit-issues.json
PYTHONPATH=projects/monolith python -m factory.orchestration.factory_gate_cleanup --audit --fixture gate-audit-issues.json > gate-audit.jsonl
```

The audit parses actual generated blocks, reports every open issue, and fails
on multiple owned or unowned anchored rescope lines, or malformed boundaries.
A single unowned rescope is reported as a warning and left unchanged. Held
issues remain in the read-only audit, including their duplicates; skipped
cleanup is not evidence that their acceptance contracts are deduplicated. Include
issues 7163, 7162, 7148, 7428, 7367, 7376 and 7250 when still open. Record their
absence as closed only after a fresh read. API failures and incomplete coverage
are errors. Keep issue 7452 open until deployment, cleanup, complete audit and
a subsequent authorized gate replacement are evidenced. Confirm receipt
history, closure veto, human holds and decision cards remain intact. A repeated
dry-run must report zero changes. This document records a procedure, not a
claim that the live audit has passed.
