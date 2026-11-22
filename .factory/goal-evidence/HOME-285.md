# HOME-285 evidence

1. The required `pr-checks` action in `buildbuddy.yaml` calls `bash projects/platform-factory/onepassword/tests/isolated.sh`. It installs the vendored operator CRD and proposed policy on Kubernetes v1.34.0 and runs `test-admission.sh` against the API server.
2. The delivery owner verifies existing items reconcile after the factory root Application syncs.
3. The review record on PR #8435 must show an APPROVED review by jomcgi for the current head before merge.

Joe's decision of 2026-11-22 (factory PR triage) on HOME-285: Decision: criterion 3 (Joe's APPROVED review covers the PR head) is removed from Done when. The goal check cannot see an approval in the diff, and the human gate already enforces it. Decisions keeps the rule that the PR lands only after Joe's APPROVED review. ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-285#comment-10353))
