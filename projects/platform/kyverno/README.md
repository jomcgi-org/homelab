# Kyverno

This wrapper chart installs Kyverno and two custom ClusterPolicies: resource
Audit validation and synchronized Secret generation for monolith workflows.
The hub Application is `projects/platform-gke/kyverno/application.yaml`; it
renders this chart with `values.yaml` in the `kyverno` namespace.

## Policies

`require-resource-requests` checks containers in Deployments, StatefulSets and
DaemonSets in `monolith` and `monolith-public`. It requires CPU and memory
requests plus a memory limit. It does not require a CPU limit. Violations appear
in PolicyReports because `validationFailureAction` is `Audit`.

`clone-monolith-workflows-secrets-v2` generates Secret copies in
`monolith-workflows`. Each rule matches its source Secret, so recreating a source
triggers cloning again. `generateExisting: true` covers existing sources and
`synchronize: true` keeps copies updated. This is a generate-only policy.

| Rule | Source namespace | Source and target Secret |
| --- | --- | --- |
| `clone-to-monolith-workflows` | `monolith` | `monolith-pg-app` |
| `clone-secrets-to-monolith-workflows` | `monolith` | `monolith-secrets` |
| `clone-chat-secrets-to-monolith-workflows` | `monolith` | `monolith-chat-secrets` |
| `clone-openrouter-to-monolith-workflows` | `monolith` | `goosecracker` |
| `clone-meta-spark-to-monolith-workflows` | `monolith` | `monolith-meta-spark` |
| `clone-dev-pg-app-to-monolith-workflows` | `monolith-dev` | `monolith-dev-pg-app` |
| `clone-r2-s3-to-monolith-workflows` | `monolith` | `monolith-r2-s3` |

The dev credential rule is retained. These rules clone whole Secrets; the chat
and provider Secrets can contain keys beyond those an individual job uses.
The two aggregation ClusterRoles in the clone template grant Secret access to
the background and admission controllers. Admission has read-only verbs;
background also has create, update and delete.

### Changing clone rules

Kyverno rejects edits or removals of existing synchronizing generate rules.
The replacement identity removes the obsolete ClickHouse and pg-dump-reader
rules from `clone-monolith-workflows-secrets` (#7072). ArgoCD is configured to
create the replacement policy and prune the former one.

The migration intends to preserve and adopt existing clone targets in place.
Rendering does not prove adoption or continued synchronization. Those require
metadata-only live verification after an authorized rollout. Do not delete
Secrets or rotate credentials to migrate a policy. The template header records
the earlier immutable-rule failures (#4713 and #4721).

## Controllers

The upstream dependency provides four controller components, separate from the
two custom policies:

- Admission evaluates admission requests and validates policies.
- Background reconciles generate and mutate-existing requests.
- Cleanup handles cleanup policies and resource TTL cleanup.
- Reports produces policy reports, including background scans.

## Configuration

Upstream dependency values belong under `kyverno.*`. Wrapper policy values such
as `requireResources` stay at the top level. The clone rules are defined directly
in `templates/clone-monolith-pg-secret-policy.yaml`.

| Wrapper value | Consumer and default |
| --- | --- |
| `requireResources.enabled` | Gates `templates/require-resources-policy.yaml`; `true` |
| `requireResources.background` | Sets the policy's background scan; `true` |
| `requireResources.targetKinds` | Sets the match kinds; Deployment, StatefulSet, DaemonSet |
| `requireResources.includeNamespaces` | Sets the match namespaces; monolith, monolith-public |

The pinned dependency reads `kyverno.admissionController.replicas` and
`podAnnotations` in its admission Deployment template. Its container resource
and security settings use `kyverno.admissionController.container.resources`
and `kyverno.admissionController.container.securityContext`. The existing
sibling `resources` and `securityContext` entries in this wrapper's values do
not configure that container. They are left unchanged here. The dependency's
config template reads `kyverno.config.resourceFilters` and
`kyverno.config.excludeKyvernoNamespace`.

Workloads set their exporter endpoints explicitly and join the collector's
service allowlist as described in [Observability](../../../docs/observability.md).

## Validation

`policy_cleanup_test.py` renders the policies and affected workflow charts with
Helm, including suspended and disabled paths. It checks the retained rule bodies,
aggregation RBAC, removed injection artifacts and explicit workflow telemetry.
The `policy_cleanup_test` Bazel target supplies the pinned Helm binary and chart
inputs. Repository chart admissibility also validates the registered manifests
against the pinned Kubernetes and CRD schemas in Linux CI.
