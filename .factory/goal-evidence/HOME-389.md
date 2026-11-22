1. `projects/factory/reviewer/review/engine.go` calls `invariantMergeBase` in both policy-loading paths.
2. `TestCompletedPassRechecksChangedInvariantBase` runs in `//projects/factory/reviewer/review:review_test`.
3. `README.md:141` links Joe McGinley to the GitHub account `jomcgi`. To check approval for the revision under review, set `head` to its full commit SHA and run the following. A matching row identifies the approving review.

   ```sh
   gh api --paginate repos/jomcgi-org/homelab/pulls/8439/reviews |
     jq --arg head "$head" '.[] | select(.user.login == "jomcgi" and .state == "APPROVED" and .commit_id == $head) | .html_url'
   ```

Joe's decision of 2026-11-22 (factory PR triage) on HOME-389: Decision: criterion 3 (Joe's APPROVED review covers the PR head) is removed from Done when. The goal check cannot see an approval in the diff, and the human gate already enforces it. Decisions keeps the rule that the PR lands only after Joe's APPROVED review. ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-389#comment-10363))
