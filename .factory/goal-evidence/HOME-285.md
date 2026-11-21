# HOME-285 evidence

1. The required `pr-checks` action in `buildbuddy.yaml` calls `bash projects/platform-factory/onepassword/tests/isolated.sh`. It installs the vendored operator CRD and proposed policy on Kubernetes v1.34.0 and runs `test-admission.sh` against the API server.
2. The delivery owner verifies existing items reconcile after the factory root Application syncs.
3. The review record on PR #8435 must show an APPROVED review by jomcgi for the current head before merge.
