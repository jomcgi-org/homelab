## Criterion 4

Check the current PR head with `gh pr view 8436 --json headRefOid`. In
`gh api --paginate repos/jomcgi-org/homelab/pulls/8436/reviews`, verify that
Joe (`jomcgi`) has a current `APPROVED` review whose `commit_id` matches
that head. The merge authority is documented in `AGENTS.md` and `REVIEW.md`.
