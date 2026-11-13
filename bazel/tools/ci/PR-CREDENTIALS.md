# Pull-only PR CI is staged

Issue [#7005](https://github.com/jomcgi-org/homelab/issues/7005) remains open.
`pr-credentials.sh` is an unwired candidate library. No active workflow sources
it. No token is provisioned and today's required `pr-checks` remains unchanged.
Same-repository PR code can still receive write-capable credentials.

## Provider behavior

Source inspected on 2026-11-13 at BuildBuddy commit
[`4db6dd74f9a43bab0d0bf3ef7aceb42a05708438`](https://github.com/buildbuddy-io/buildbuddy/tree/4db6dd74f9a43bab0d0bf3ef7aceb42a05708438):

- [`service.go`](https://github.com/buildbuddy-io/buildbuddy/blob/4db6dd74f9a43bab0d0bf3ef7aceb42a05708438/enterprise/server/workflow/service/service.go):
  `isTrustedCommit` trusts non-fork commits; `getWorkflowActions` requests
  default-branch config only for untrusted commits. `createActionForWorkflow`
  selects `include-secrets=true` for trusted executions with a secret service,
  appends action environment and platform properties, and enables runner
  recycling and workspace preservation. These are no event-scoped secret allowlist.
- [`config.go`](https://github.com/buildbuddy-io/buildbuddy/blob/4db6dd74f9a43bab0d0bf3ef7aceb42a05708438/enterprise/server/workflow/config/config.go):
  `Action` supports `env`, `platform_properties`, `pool` and `self_hosted`.
  It exposes no event-scoped secret-selection field. An action name or a
  PR-editable property cannot establish trust.
- [`ci_runner/main.go`](https://github.com/buildbuddy-io/buildbuddy/blob/4db6dd74f9a43bab0d0bf3ef7aceb42a05708438/enterprise/server/cmd/ci_runner/main.go):
  `repoCredentialHelperConfig` uses `REPO_TOKEN` for Git HTTPS authentication;
  local and global helpers also cover dependency checkouts. Removing the
  explicit GHCR login leaves this separate credential path.
- [`snaputil.go`](https://github.com/buildbuddy-io/buildbuddy/blob/4db6dd74f9a43bab0d0bf3ef7aceb42a05708438/enterprise/server/remote_execution/snaputil/snaputil.go):
  `UniversalSnapshotRef` is a fallback writable from any branch. Existing
  default-branch fallback and mutable workspace reuse need provider-enforced
  separation in both directions.

This source inspection establishes upstream behavior, not the deployed service
version or an account-policy attestation. No verified supported account control
for the entire required boundary was available. Activation stays blocked until
the provider establishes that capability with public, redacted evidence.

## Candidate behavior

`candidate_role EVENT PR_NUMBER BRANCH` rejects absent or malformed routing data.
PR events win even for a fork head named `main`. Every argument can be spoofed
by PR code. The returned role grants no authority.

`candidate_pr_login pr` accepts only `GHCR_PULL_TOKEN`, refuses known write-token
inputs (including coexistence with the pull token), and requires an empty
`DOCKER_CONFIG`. It never falls back to `GHCR_TOKEN` or logs into Docker Hub.
The provider must constrain the credential to GHCR pulls; shell cannot infer
its scopes. Empty Docker config is a diagnostic, not proof of isolated disk,
credential helpers, cached layers, inherited memory or other injected secrets.

`candidate_pr_drift pr [NEW_BUILD_FILES...]` fails on tracked formatting or
generated drift and on newly generated BUILD paths. It reports the source
repair commands and never commits, changes remotes or pushes. The future
adapter must replace **both** Gazelle and final format writeback calls, and
remove the format-bot bypass in restricted mode so a PR cannot skip validation
by choosing commit metadata. Source authors still generate and commit files.

The existing `shared_runners_test` harness executes the library with inert
sentinels, mocked Docker and a temporary Git repository. Its existing tests
continue to execute PR, queue and main test dispatch. These regressions cannot
prove external secret policy, token scope or snapshot isolation.

## Activation and rollback

Separate authority is required to provision credentials and activate provider
policy. Before wiring this library:

1. Verify a provider-controlled event classification before checkout or any
   PR-controlled code. Same-repository and fork PRs receive only dedicated
   pull-only GHCR auth and appropriately restricted build/cache access. Deny
   repository-write and registry-push credentials, checkout/helper tokens,
   Docker Hub credentials and every other write-capable injected secret.
2. Enforce that policy outside PR YAML and code. Deny PR-requested
   `include-secrets=true`, including the transitive `ci test` remote-run path,
   arbitrary environment/platform overrides and alternate pools. Today's
   `ci` explicitly requests all secrets; it must not be reachable with a
   privileged identity from the restricted lane.
3. Partition or disable snapshots, fallback/default-branch/universal reuse,
   runner recycling, Docker config, Git helpers, homes, workspaces and mutable
   executable/cache state across privilege classes. Privileged queue/main
   must never restore PR-written executable state; PRs must never restore
   privileged credentials. Invalidate existing shared snapshots at cutover.
   A changed resource request or action name is insufficient.
4. Wire the candidate auth and drift calls through a provider-controlled
   entrypoint. Preserve `pr-checks`, generators, affected tests, image-build
   and render gates; preserve full Linux queue validation without publishing
   and main image/chart publication and authenticated chart writeback.
5. Record the policy audit, runner-policy evidence, restricted PR invocation,
   queue invocation and main publication receipt on #7005. Missing evidence
   fails acceptance. Keep dependency author lists and publisher activation off.

Rollback repository wiring by reverting the activation PR. If isolation fails,
stop restricted executions at the provider and keep dependency intake disabled;
never restore write credentials to PRs as a fallback. Provider owners invalidate
contaminated state before resuming. This staged PR changes no live settings.
