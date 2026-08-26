# k8s-schemas: vendored Kubernetes JSON schemas for the admissibility gate

Hermetic input for //bazel/helm:chart_admissibility_test (#4831). kubeconform
validates every rendered manifest against these files at test time. No network
access happens during the test: the CI sandbox cannot resolve DNS, and the
first version of this gate downloaded schemas from raw.githubusercontent.com,
which made it fail (and prove nothing) in CI.

## What is pinned here

| Field | Value |
| ----- | ----- |
| Upstream repo | https://github.com/yannh/kubernetes-json-schema |
| Pinned commit | `5a69f8365c9d3ed7de997f5365e22481cf775fa2` (master, 2026-08-21) |
| Directory | `v1.35.8-standalone-strict/` |
| Schema flavour | standalone-strict (self-contained files, `additionalProperties: false`) |
| Cluster minor | Kubernetes 1.35 (see ADR agents/028, "the cluster is 1.35") |

Upstream publishes no release tarballs (no GitHub releases; one ancient tag),
and a tarball of the whole master branch is over 250 MB because it carries
schemas for every k8s version since 1.6 in four flavours. Vendoring the whole
archive would be far too heavy for CI caches, so this directory carries only
the schemas for the built-in kinds this repository's charts actually render
(46 kind/apiVersion pairs across the 28 manifest sets as of vendoring), pruned
from the pinned commit's `v1.35.8-standalone-strict/` directory.

`SHA256SUMS` pins the sha256 of every vendored file, and
admissibility-test.sh verifies the tree against that manifest before running
kubeconform. To re-verify locally:

    cd bazel/helm/k8s-schemas && sha256sum -c SHA256SUMS   # or: shasum -a 256 -c SHA256SUMS

Aggregate tree digest at vendoring time
(`shasum -a 256 SHA256SUMS v1.35.8-standalone-strict/*.json | shasum -a 256`):
`86f2de5589df39fab64df1279de23e3e3a6fa5a06148366fcdb10abc4555e6db`

## CRD kinds are skipped by name, not silently

The vendored set covers Kubernetes built-in kinds only. Custom resources have
no schema here, and kubeconform treats a missing schema as an Error, so every
rendered CRD must appear explicitly on the test's skip list (matched as
`<apiVersion>/<Kind>`, which also distinguishes the two different ClusterPolicy
kinds from kyverno and nvidia). The skip list lives in
bazel/helm/admissibility-test.sh with per-kind rationale.

Deliberately NOT used: `-ignore-missing-schemas`. It would silently turn any
kind without a schema into a no-op validation, which is exactly the failure
class this gate exists to close.

When a chart starts rendering a new kind:

- Built-in kind: copy `<kind>-<group>-<version>.json` for the matching
  apiVersion from the pinned upstream commit/directory above into
  `v1.35.8-standalone-strict/`, regenerate `SHA256SUMS`, and commit both.
- CRD: add its exact `<apiVersion>/<Kind>` to the documented skip list.
Either way the test fails loudly until you decide, so nothing new slips past
the gate unvalidated.

To move to a newer cluster minor (say 1.36): vendor the equivalent directory
from a fresh upstream commit, update `-kubernetes-version` in
admissibility-test.sh, and refresh the skip list if CRDs changed.

## Why standalone-strict

"Standalone" files inline all referenced definitions, so each file validates
independently with no $ref resolution (and no network). "Strict" files reject
unknown fields (`additionalProperties: false`), which is what catches
values-layer defects such as #4830 before they reach the API server.
