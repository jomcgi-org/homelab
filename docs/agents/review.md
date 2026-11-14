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
