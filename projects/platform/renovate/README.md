# Renovate

Renovate runs on the factory home cluster through the
[factory Application](../../platform-factory/renovate/application.yaml).
It loads `values.yaml` then `values-factory.yaml` and renders a Kubernetes
CronJob in namespace `renovate`. The factory has no Argo Workflows.

**The CronJob ships suspended.** After this change merges, Claude verifies
one manual run as `jomcgi-factory-renovate[bot]`, including verified commits,
before #8041 (rebased or replaced) sets `suspend: false` in
`values-factory.yaml`. The hub's two suspended CronWorkflows remain in place;
their removal is a follow-up to
[#7945](https://github.com/jomcgi-org/homelab/issues/7945). The separate weekly
maintenance helper is not deployed on the factory cluster.

## GitHub credential

The factory OnePasswordItem `renovate/factory-renovate-gh-app` uses
`vaults/k8s-homelab/items/factory-renovate-gh-app`. Joe created the
`jomcgi-factory-renovate` GitHub App on 2026-11-15 and installed it only on
`jomcgi-org/homelab`. It has write access to contents, pull requests, workflows,
issues, commit statuses and merge queues, and read access to checks,
Dependabot alerts and metadata. It is absent from the ruleset's bypass list.
The SSH Key item's `private key` field becomes Secret key `private-key`,
alongside text fields `app-id` and `installation-id`, matching
[the factory App's layout](../../factory/ARCHITECTURE.md#inputs-from-joe).

Each Pod's `github-app-token` init container uses Node from the same Renovate
image. It signs an RS256 App JWT with `iat` 60 seconds back and `exp` nine
minutes ahead, then calls `POST /app/installations/{installation-id}/access_tokens`
for `homelab`. It resolves the bot ID with
`GET /users/jomcgi-factory-renovate%5Bbot%5D` and checks the returned login
and type before constructing
`jomcgi-factory-renovate[bot] <id+jomcgi-factory-renovate[bot]@users.noreply.github.com>`.
This lookup uses GitHub's bot user ID, which differs from the App ID.

Only the init container mounts the App Secret. It writes the token and author
to owner-readable files in a memory-backed emptyDir. Renovate mounts that
volume read-only, loads `RENOVATE_TOKEN` and `RENOVATE_GIT_AUTHOR` at startup,
and runs with `username: jomcgi-factory-renovate[bot]` and
`platformCommit: enabled`. Commits go through GitHub's API for verification.
The init script never logs the key, JWT, token, API response bodies or raw
errors. Authentication or bot lookup failure prevents Renovate from starting.

The hub retains its existing `renovate-github` OnePasswordItem and PAT
reference. This change neither edits that item nor deletes the PAT. After
the manual run is verified and the factory schedule is enabled, Joe deletes
the old PAT and `k8s-homelab/renovate-github`.

## Operations

The scan runs daily at 04:00 America/Vancouver with `concurrencyPolicy: Forbid`.
Each Job has a 45-minute deadline and two retries. The deadline includes init
time and retries and stays below the installation token's one-hour lifetime.
Kubernetes retains one
successful Job and up to five failed Jobs, with finished Jobs expiring after
one day. The workspace is a disposable 4 GiB emptyDir.

The runner requests and limits memory at 4 GiB. The repository has more than
500 extracted dependencies; a 2 GiB limit was repeatedly killed during
registry metadata resolution. It runs as the image's non-root UID 12021
with a read-only root filesystem, no extra capabilities and no mounted
Kubernetes API token.

Renovate targets only `jomcgi-org/homelab`, requires the checked-in
`renovate.json`, and disables autodiscovery and onboarding. Package lifecycle
scripts and plugins are disabled. The sole allowlisted post-upgrade command,
`bazel/tools/format/update-python-requirements.sh`, regenerates the root
compiled Python locks. Native uv artifact commands receive `UV_NO_BUILD=true`;
the compiler also refuses source builds. The existing `bazelModDeps`
exception is retained for Bazel lock maintenance.

Ordinary PR creation remains before 06:00 Monday in America/Vancouver, with
five concurrent PRs, two per hour and a three-day release age. A daily scan
does not expand that PR creation window. Security alerts retain their
immediate exception. Dependabot security updates remain enabled.
See [coverage and holds](COVERAGE.md) for exclusions and generated files.

## Review and auto-merge

`renovate.json` enables native platform auto-merge for minor, patch, digest,
pin (including digest pins) and lock-file maintenance updates. Major updates
stay manual and carry `major-upgrade`. Replacement, bump and rollback updates
also keep the default manual merge policy. Existing compatibility holds
still disable auto-merge and require dashboard approval.

GitHub must require both `pr-checks` and the reviewer App's `factory review`
before a PR can enter the merge queue. Renovate requests auto-merge and never
approves its own PR. This change does not edit rulesets, reviewer permissions
or bypasses. Before running the writer, verify those required checks and
repository auto-merge are enabled. The policy applies to any runner reading
this repository, even while this CronJob is suspended.

The hub's weekly maintenance helper retains its separate default-off
`MAINTENANCE_AUTOMERGE_ENABLED` flag. Its old dependency-evidence rollout
instructions have been superseded for this factory deployment by #7945;
this change does not claim to resolve the separate CI credential isolation
work in [#7005](https://github.com/jomcgi-org/homelab/issues/7005).

## Network

The [namespace policy](../../platform-factory/renovate/resources/network-policy.yaml)
selects only Renovate pods and permits TCP 443 to GitHub and the registries
used by the enabled managers: npm, PyPI, the Go proxy and checksum service,
GHCR, Docker Hub, Quay, ECR, Chainguard, registry.k8s.io, Bazel Central Registry,
and the Helm repositories declared in this repository. It includes their
artifact hosts and Node, Go, Bazel and Helm toolchain downloads.
Cilium selects the pod by its `app.kubernetes.io/name: renovate` and
`app.kubernetes.io/instance: renovate` labels. Its existing `api.github.com`
TCP 443 grant also covers the init container, which shares the pod's network
endpoint. Cilium's cluster baseline supplies DNS to CoreDNS through its DNS proxy.
There is no ingress, world egress or Kubernetes API grant.

Add a registry's DNS names with the dependency that first needs it.
`*.githubusercontent.com` and `*.quay.io` cover those providers' artifact
hosts; `*-docker.pkg.dev` covers registry.k8s.io's regional redirects.
The other entries are exact names. These are DNS/IP and port rules,
not URL-path filters. Check Cilium drops during the first run for any new
upstream redirect before proposing a narrower source addition.

## Verify one run

After merge, wait for ArgoCD's `renovate` Application to be Synced and Healthy,
its OnePasswordItem to materialize `private-key`, `app-id` and `installation-id`
in Secret `factory-renovate-gh-app`, and its Cilium policy to exist. Check key
names only; do not display Secret values. Confirm `pr-checks` and `factory review`
are required, repository auto-merge is enabled, and this App has no bypass.
Keep `suspend: true` during verification.

Ensure no other Renovate Job is running. Manually created Jobs bypass the
CronJob's concurrency policy.

```bash
export KUBECONFIG=~/.kube/factory.yaml
kubectl -n renovate get cronjob renovate
kubectl -n renovate get jobs
run="renovate-manual-$(date +%s)"
KUBECONFIG=~/.kube/factory.yaml kubectl -n renovate create job --from=cronjob/renovate "$run"
kubectl -n renovate logs --follow --pod-running-timeout=10m "job/$run" -c renovate
kubectl -n renovate wait --for=condition=complete "job/$run" --timeout=46m
```

Confirm the init container exits zero and extraction completes without
authentication failures, OOM kills or registry timeouts. If initialization
fails, inspect `kubectl -n renovate logs "job/$run" -c github-app-token` and
pod events; do not dump the Secret or token volume.

Outside Monday's PR window, a successful scan may create no ordinary PR;
that alone does not verify PR authorship or signed commits. For any PR the
run opens, verify author `jomcgi-factory-renovate[bot]` and every commit's
`verification.verified: true`:

```bash
gh pr view <pr-number> --repo jomcgi-org/homelab --json author,autoMergeRequest,headRefOid
gh api repos/jomcgi-org/homelab/pulls/<pr-number>/commits \
  --jq '.[] | {sha, author: .author.login, verified: .commit.verification.verified, reason: .commit.verification.reason}'
```

For an eligible non-major PR, verify its native auto-merge request, both
required checks on its current head and its queue result. A major PR must
stay manual. Record the Job and PR evidence before enabling the daily
schedule through #8041 or its replacement. Suspending the CronJob stops future
scheduled Jobs; it does not stop a running Job or cancel an armed PR.

## Repository checks

`ci` is the local validation entry point. CI runs the Bazel targets
`//projects/platform/renovate:dependency_policy_test`,
`//projects/platform/renovate:factory_deployment_test` and
`//projects/platform/renovate:hub_deployment_test`, plus Helm rendering and
linting for the factory Application. The policy tests cover extraction,
manual majors, retained holds and native auto-merge without approval.
The deployment tests follow the actual ArgoCD roots and values order,
check the App item, init container, memory-backed token handoff, absence of a
Secret-backed `RENOVATE_TOKEN`, deadline and pod egress, and retain the hub's
suspended render checks. The policy suite executes the init script with a
generated RSA key and mocked GitHub responses to check JWT claims/signatures,
bot identity, file permissions and credential-safe failure handling.

These checks validate repository configuration. Credential acceptance,
network reachability and the first run still require live verification.
