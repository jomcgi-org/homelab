1. `TestParseAppliesDefaults` in `projects/factory/reviewer/config/config_test.go`
   checks the parsed binding's full path list. Run
   `bazel test //projects/factory/reviewer/config:config_test`.
   The matcher in `projects/factory/reviewer/review/gate.go:411` uses that list.
2. `REVIEW.md:270-279` under “Joe decides” lists the same ten entries as
   `projects/factory/reviewer/config/config.go:138-147`. Both lists start with
   `projects/factory/deploy/values.yaml` and end with `renovate.json`.
3. For PR #8432, compare the head SHA from `gh pr view 8432 --json headRefOid`
   with the review commit IDs from
   `gh api --paginate repos/jomcgi-org/homelab/pulls/8432/reviews --jq '.[] | select(.user.login == "jomcgi" and .state == "APPROVED") | .commit_id'`.
