# Renovate

Renovate runs on the factory home cluster through the
[factory Application](../../platform-factory/renovate/application.yaml).
It loads `values.yaml` then `values-factory.yaml` and renders a Kubernetes
CronJob in namespace `renovate`. The factory has no Argo Workflows.

**The CronJob ships suspended.** Joe verifies the existing credential before
the first manual run or a PR setting `suspend: false` in
`values-factory.yaml`. The hub's two suspended CronWorkflows remain in place;
their removal is a follow-up to
[#7945](https://github.com/jomcgi-org/homelab/issues/7945). The separate weekly
maintenance helper is not deployed on the factory cluster.

## GitHub credential

The OnePasswordItem `renovate/renovate-github` uses
`vaults/k8s-homelab/items/renovate-github`. Its Secret must contain the field
`RENOVATE_TOKEN`, which the runner reads by secret reference. No token or App
is created here.

The item's type, identity, permissions and expiry/renewal are unverified.
Before rollout, Joe confirms it can access only the intended repository,
`jomcgi-org/homelab`, with **contents, pull requests and workflows write**,
plus the ability to **enable auto-merge**. Check the existing identity against
Renovate's [GitHub authentication requirements](https://docs.renovatebot.com/modules/platform/github/);
App and token permission names differ. Confirm any required merge-queue
capability for that identity. Do not infer permissions from an item name or
a synced Application, and do not print Secret values during verification.

## Operations

The scan runs daily at 04:00 America/Vancouver with `concurrencyPolicy: Forbid`.
Each Job has a two-hour deadline and two retries. Kubernetes retains one
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

Ordinary PR creation defaults to before 06:00 Monday in America/Vancouver,
with five concurrent PRs, two per hour and a three-day release age. Security
alerts retain their immediate exception. Dependabot security updates remain
enabled.
See [coverage and holds](COVERAGE.md) for exclusions and generated files.

OpenTelemetry and Pydantic/core family PRs can be created on any scan,
including ordinary updates. Renovate 44.14.10 takes a grouped branch's
schedule from its first dependency, so an unaffected peer must also be
eligible outside Monday. Ordinary family updates use the normal creation
limits and release-age status checks. Security alert members contribute
explicit unlimited PR, branch and hourly commit limits; Renovate's grouped
limit calculation honors an unlimited member even when it sorts after an
unaffected dependency.

## Review and auto-merge

`renovate.json` normally enables native platform auto-merge for minor, patch,
digest, pin (including digest pins) and lock-file maintenance updates. Major updates
stay manual and carry `major-upgrade`. Replacement, bump and rollback updates
also keep the default manual merge policy. Existing compatibility holds
still disable auto-merge and require dashboard approval.

OpenTelemetry and Pydantic/core use Renovate's own merge checks
(`platformAutomerge: false`). Their PRs can open while a release is pending,
but Renovate waits for `renovate/stability-days` to pass before requesting a
merge. GitHub platform auto-merge would not enforce that status because it
is not a required repository check. A security alert exempts the vulnerable
package from the cooldown; unaffected peers still wait three days. The
security group's immediate creation schedule and unlimited quotas remain.

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
Cilium's cluster baseline supplies DNS to CoreDNS through its DNS proxy.
There is no ingress, world egress or Kubernetes API grant.

Add a registry's DNS names with the dependency that first needs it.
`*.githubusercontent.com` and `*.quay.io` cover those providers' artifact
hosts; `*-docker.pkg.dev` covers registry.k8s.io's regional redirects.
The other entries are exact names. These are DNS/IP and port rules,
not URL-path filters. Check Cilium drops during the first run for any new
upstream redirect before proposing a narrower source addition.

## Verify one run

After Joe confirms the credential and required checks, wait for ArgoCD's
`renovate` Application to be Synced and Healthy, its OnePasswordItem to
materialize the expected key, and its Cilium policy to exist. Verify only
secret synchronization and key presence, without displaying the token.
Keep `suspend: true` during verification.

Ensure no other Renovate Job is running. Manually created Jobs bypass the
CronJob's concurrency policy.

```bash
export KUBECONFIG=~/.kube/factory.yaml
kubectl -n renovate get cronjob renovate
kubectl -n renovate get jobs
run="renovate-manual-$(date +%s)"
kubectl -n renovate create job --from=cronjob/renovate "$run"
kubectl -n renovate logs --follow --pod-running-timeout=10m "job/$run"
kubectl -n renovate wait --for=condition=complete "job/$run" --timeout=2h
```

Confirm extraction completes without authentication failures, OOM kills or
registry timeouts. Outside Monday's PR window, a successful scan may create
no ordinary PR. For an eligible non-major PR, verify its author and native
auto-merge request, then both required checks on its current head and its
queue result. A major PR must stay manual. Enable the daily schedule through
a values PR only after that verification. Suspending the CronJob stops future
scheduled Jobs; it does not stop a running Job or cancel an armed PR.

## Repository checks

`ci` is the local validation entry point. CI runs the Bazel targets
`//projects/platform/renovate:dependency_policy_test`,
`//projects/platform/renovate:factory_deployment_test` and
`//projects/platform/renovate:hub_deployment_test`, plus Helm rendering and
linting for the factory Application. The policy tests cover extraction,
manual majors, retained holds and native auto-merge without approval.
The deployment tests follow the actual ArgoCD roots and values order,
check the factory's credential, runtime limits and egress, and retain the
hub's suspended render checks.

These checks validate repository configuration. Credential acceptance,
network reachability and the first run still require live verification.
