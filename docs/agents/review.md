# Review

What to check and how to rate it is in the root [`REVIEW.md`](../../REVIEW.md),
with every `REVIEW.md` that applies to the changed paths. This page is the
process.

The reviewer is independent of the author. It reports findings without
editing, committing, pushing, running `ci` or executing anything from the PR.
Independent agent review satisfies the review step under `AGENTS.md` "Merge
authority"; routine human sign-off is unnecessary outside `REVIEW.md` "Joe
decides".

The first review covers the whole diff at the head SHA. Later heads follow
`REVIEW.md` "Later heads". The landing owner confirms that the review and the
required checks cover the current head: the reviewer service for the
`factory review` check, the dispatcher on the hub factory. A passing review
permits landing. Whether and when to land stays with the landing owner, and
a hold on landing still applies.

Factory work authored by `jomcgi-factory[bot]` needs Joe's APPROVED review
when it touches a gated path or has a `human-gate` finding. Sessions Joe
guides as `jomcgi` may use approval by authorship under his
[decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6891640540)
and the [#8049 scope](https://github.com/jomcgi-org/homelab/issues/8049). Such
a session lands a gated change only when Joe asked for it in that session.
The PR must be authored by a `humanApprovers` account, with its head in the base
repository and every push since the latest branch creation by those accounts.
The reviewer rechecks complete activity within 100 events on every poll and
requires the newest push to match the head. A CI format commit pushed with
Joe's token counts as his push.

The waiver verifies the GitHub accounts behind the PR and its pushes.
Joe's decision also covers other traffic using his account. GitHub activity
does not attest who supervised a session; the session remains responsible
for following Joe's request to land. HOME-56 excludes Renovate from this
account-based permission.

Renovate never gets this waiver: `renovate/` branches and commits with its
git author email are excluded, using base `renovate.json` `gitAuthor` when
set. Both `renovate@whitesourcesoftware.com` and the maintenance workflow's
`renovate[bot]@users.noreply.github.com` remain excluded when `gitAuthor` is
configured. Renovate needs a separate identity without admin permissions;
this exclusion applies until then. Hold labels, shared-head and foreign-check
holds still need an APPROVED review. A reviewer restart adds no hold. Other
findings and required checks still block, and eligible PRs use the merge queue
through requested auto-merge.

## Changing rules

Prefer a CI check to a rule (`docs/agents/evolution.md`). A rule is for what
a reviewer can check from a diff and CI cannot yet.

- A rule is one bullet: a bold name, what the diff must show, the failure it
  prevents, and the PR or issue it came from. It may name a severity.
- The PR adding a rule gives one change the rule must still allow, and
  checks the rules already on the path for a duplicate or a conflict.
- A `REVIEW.md` holds at most twelve rules. A thirteenth replaces or merges
  one.
- A rule is removed in the PR that adds the check enforcing it or deletes
  the code it guards. The owner of the domain's `ARCHITECTURE.md` prunes
  rules that produced no finding in 90 days and whose source failure a test
  now covers.
