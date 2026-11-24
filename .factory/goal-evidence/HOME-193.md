5. `TestHumanGateByPathHoldsUntilApprovalOnTheHead` in
   `projects/factory/reviewer/review/engine_test.go` exercises the gate for
   a factory-authored PR changing the reviewer. It checks that approval of
   an older commit and approval by a different user both leave the hold in
   place. The Bazel target is `//projects/factory/reviewer/review:review_test`.

   For the live review record, run the following against GitHub and compare
   `.head.sha` with the review commit from `jomcgi`. Approval evidence is an
   effective `APPROVED` review on that exact commit.

   ```sh
   gh api repos/jomcgi-org/homelab/pulls/8438 --jq '.head.sha'
   gh api --paginate repos/jomcgi-org/homelab/pulls/8438/reviews \
     --jq '.[] | select(.user.login == "jomcgi") | {id, state, commit_id, submitted_at}'
   ```
