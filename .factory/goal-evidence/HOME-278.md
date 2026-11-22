1. `TestParseAppliesDefaults` in `projects/factory/reviewer/config/config_test.go`
   checks the parsed binding's full path list. Its Bazel target is
   `//projects/factory/reviewer/config:config_test`.
   The matcher in `projects/factory/reviewer/review/gate.go:411` uses that list.
2. `REVIEW.md:299-308` under “Joe decides” lists the same ten entries as
   `projects/factory/reviewer/config/config.go:138-147`. Both lists start with
   `projects/factory/deploy/values.yaml` and end with `renovate.json`.
3. The approval record for [PR #8432](https://github.com/jomcgi-org/homelab/pull/8432)
   is a GitHub review by `jomcgi` with state `APPROVED` and a commit ID matching the PR head.
