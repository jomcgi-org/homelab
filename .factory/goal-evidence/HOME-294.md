## Criterion 4

Check the current PR head with `gh pr view 8436 --json headRefOid`. In
`gh api --paginate repos/jomcgi-org/homelab/pulls/8436/reviews`, verify that
Joe (`jomcgi`) has a current `APPROVED` review whose `commit_id` matches
that head. The merge authority is documented in `AGENTS.md` and `REVIEW.md`.

Joe's decision of 2026-11-22 (factory PR triage) on HOME-294: Decision: criterion 4 (Joe's APPROVED review covers the PR head) is removed from Done when. The goal check cannot see an approval in the diff, and the human gate already enforces it. Decisions keeps the rule that the PR lands only after Joe's APPROVED review. ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-294#comment-10355))
