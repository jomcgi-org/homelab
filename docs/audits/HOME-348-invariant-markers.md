# HOME-348 invariant marker coverage

This inventory records the live and unproven rows at commit `7a989d5ae`. Each row links its catalog entry to the marker in each mechanism file. For live rows, the test links show the Bazel target declaration and the marked source in its srcs. PLAT-2 names the edge chart directory. Its authentication policy lives in the SecurityPolicy template.

## `INVARIANTS.yaml`

| Record | State | Mechanism markers | Test marker |
| --- | --- | --- | --- |
| [ROOT-1](../../INVARIANTS.yaml#L5) | live | [chart_version_guard.py](../../bazel/tools/ci/chart_version_guard.py#L2) | [`//bazel/tools/ci:chart_version_guard_test`](../../bazel/tools/ci/BUILD#L62) → [chart_version_guard_test.py](../../bazel/tools/ci/chart_version_guard_test.py#L1) |

## `projects/factory/INVARIANTS.yaml`

| Record | State | Mechanism markers | Test marker |
| --- | --- | --- | --- |
| [FACT-1](../../projects/factory/INVARIANTS.yaml#L5) | unproven | [_binding.tpl](../../projects/factory/chart/templates/_binding.tpl#L1) | — |
| [FACT-2](../../projects/factory/INVARIANTS.yaml#L10) | unproven | [_binding.tpl](../../projects/factory/chart/templates/_binding.tpl#L2) | — |
| [FACT-3](../../projects/factory/INVARIANTS.yaml#L15) | live | [app.go](../../projects/factory/broker/github/app.go#L1) | [`//projects/factory/broker/github:github_test`](../../projects/factory/broker/github/BUILD#L23) → [github_test.go](../../projects/factory/broker/github/github_test.go#L1) |
| [FACT-4](../../projects/factory/INVARIANTS.yaml#L21) | live | [config.go](../../projects/factory/broker/config/config.go#L1) | [`//projects/factory/broker/config:config_test`](../../projects/factory/broker/config/BUILD#L12) → [config_test.go](../../projects/factory/broker/config/config_test.go#L1) |
| [FACT-5](../../projects/factory/INVARIANTS.yaml#L27) | live | [verifier.go](../../projects/factory/broker/actorjwt/verifier.go#L1) | [`//projects/factory/broker/actorjwt:actorjwt_test`](../../projects/factory/broker/actorjwt/BUILD#L11) → [verifier_test.go](../../projects/factory/broker/actorjwt/verifier_test.go#L1) |
| [FACT-6](../../projects/factory/INVARIANTS.yaml#L33) | unproven | [network-policy.yaml](../../projects/factory/chart/templates/network-policy.yaml#L1) | — |
| [FACT-7](../../projects/factory/INVARIANTS.yaml#L38) | unproven | [broker-admission.yaml](../../projects/factory/chart/templates/broker-admission.yaml#L1) | — |
| [FACT-8](../../projects/factory/INVARIANTS.yaml#L43) | unproven | [broker-admission.yaml](../../projects/factory/chart/templates/broker-admission.yaml#L2) | — |
| [FACT-9](../../projects/factory/INVARIANTS.yaml#L48) | live | [rules.go](../../projects/factory/reviewer/review/rules.go#L1) | [`//projects/factory/reviewer/review:review_test`](../../projects/factory/reviewer/review/BUILD#L35) → [rules_test.go](../../projects/factory/reviewer/review/rules_test.go#L1) |
| [FACT-10](../../projects/factory/INVARIANTS.yaml#L54) | unproven | [delivery.go](../../projects/factory/connector/dispatch/delivery.go#L1), [engine.go](../../projects/factory/reviewer/review/engine.go#L1) | — |
| [FACT-11](../../projects/factory/INVARIANTS.yaml#L60) | unproven | [delivery.go](../../projects/factory/connector/dispatch/delivery.go#L2), [gate.go](../../projects/factory/reviewer/review/gate.go#L1) | — |

## `projects/factory-cluster/INVARIANTS.yaml`

| Record | State | Mechanism markers | Test marker |
| --- | --- | --- | --- |
| [CLUS-1](../../projects/factory-cluster/INVARIANTS.yaml#L5) | unproven | [k3s-config.yaml](../../projects/factory-cluster/nodes/k3s-config.yaml#L1) | — |
| [CLUS-2](../../projects/factory-cluster/INVARIANTS.yaml#L10) | unproven | [k3s-config.yaml](../../projects/factory-cluster/nodes/k3s-config.yaml#L2) | — |
| [CLUS-3](../../projects/factory-cluster/INVARIANTS.yaml#L15) | unproven | [registries.yaml](../../projects/factory-cluster/nodes/registries.yaml#L1) | — |

## `projects/platform-factory/INVARIANTS.yaml`

| Record | State | Mechanism markers | Test marker |
| --- | --- | --- | --- |
| [PLAT-1](../../projects/platform-factory/INVARIANTS.yaml#L5) | unproven | [baseline.yaml](../../projects/platform-factory/network-policy/resources/baseline.yaml#L1) | — |
| [PLAT-2](../../projects/platform-factory/INVARIANTS.yaml#L10) | live | [security-policy.yaml](../../projects/platform-factory/edge/chart/templates/security-policy.yaml#L1) | [`//projects/platform-factory/edge/chart:edge_test`](../../projects/platform-factory/edge/chart/BUILD#L24) → [edge_test.py](../../projects/platform-factory/edge/chart/edge_test.py#L1) |
| [PLAT-3](../../projects/platform-factory/INVARIANTS.yaml#L17) | unproven | [policies.yaml](../../projects/platform-factory/edge-admission/resources/policies.yaml#L1) | — |
| [PLAT-5](../../projects/platform-factory/INVARIANTS.yaml#L26) | unproven | [kagent-identity-guard.yaml](../../projects/platform-factory/substrate/resources/kagent-identity-guard.yaml#L1) | — |
| [PLAT-6](../../projects/platform-factory/INVARIANTS.yaml#L31) | unproven | [network-policy.yaml](../../projects/platform-factory/substrate/resources/network-policy.yaml#L1) | — |
| [PLAT-7](../../projects/platform-factory/INVARIANTS.yaml#L36) | unproven | [argocd.yaml](../../projects/platform-factory/network-policy/resources/argocd.yaml#L1), [onepassword.yaml](../../projects/platform-factory/network-policy/resources/onepassword.yaml#L1) | — |
| [PLAT-8](../../projects/platform-factory/INVARIANTS.yaml#L42) | unproven | [claim-admission.yaml](../../projects/platform-factory/garage/resources/claim-admission.yaml#L1) | — |
| [PLAT-13](../../projects/platform-factory/INVARIANTS.yaml#L63) | live | [BUILD](../../bazel/helm/content_rules/BUILD#L1), [content_rules.py](../../bazel/helm/content_rules/content_rules.py#L2), [helm-template-test.sh](../../bazel/helm/helm-template-test.sh#L2) | [`//bazel/helm/content_rules:content_rules_test`](../../bazel/helm/content_rules/BUILD#L38) → [content_rules_test.py](../../bazel/helm/content_rules/content_rules_test.py#L1) |

## `projects/shared/helm/factory-library/INVARIANTS.yaml`

| Record | State | Mechanism markers | Test marker |
| --- | --- | --- | --- |
| [LIB-1](../../projects/shared/helm/factory-library/INVARIANTS.yaml#L5) | live | [_workloads.tpl](../../projects/shared/helm/factory-library/chart/templates/_workloads.tpl#L1) | [`//projects/shared/helm/factory-library:library_test`](../../projects/shared/helm/factory-library/BUILD#L13) → [library_test.py](../../projects/shared/helm/factory-library/library_test.py#L1) |
| [LIB-2](../../projects/shared/helm/factory-library/INVARIANTS.yaml#L12) | live | [_helpers.tpl](../../projects/shared/helm/factory-library/chart/templates/_helpers.tpl#L1) | [`//projects/shared/helm/factory-library:library_test`](../../projects/shared/helm/factory-library/BUILD#L13) → [library_test.py](../../projects/shared/helm/factory-library/library_test.py#L2) |
| [LIB-3](../../projects/shared/helm/factory-library/INVARIANTS.yaml#L18) | live | [_workloads.tpl](../../projects/shared/helm/factory-library/chart/templates/_workloads.tpl#L2) | [`//projects/shared/helm/factory-library:library_test`](../../projects/shared/helm/factory-library/BUILD#L13) → [library_test.py](../../projects/shared/helm/factory-library/library_test.py#L3) |
| [LIB-4](../../projects/shared/helm/factory-library/INVARIANTS.yaml#L24) | live | [_workloads.tpl](../../projects/shared/helm/factory-library/chart/templates/_workloads.tpl#L3) | [`//projects/shared/helm/factory-library:library_test`](../../projects/shared/helm/factory-library/BUILD#L13) → [library_test.py](../../projects/shared/helm/factory-library/library_test.py#L4) |
| [LIB-5](../../projects/shared/helm/factory-library/INVARIANTS.yaml#L30) | live | [_helpers.tpl](../../projects/shared/helm/factory-library/chart/templates/_helpers.tpl#L2), [_workloads.tpl](../../projects/shared/helm/factory-library/chart/templates/_workloads.tpl#L4) | [`//projects/shared/helm/factory-library:library_test`](../../projects/shared/helm/factory-library/BUILD#L13) → [library_test.py](../../projects/shared/helm/factory-library/library_test.py#L5) |
| [LIB-6](../../projects/shared/helm/factory-library/INVARIANTS.yaml#L37) | live | [BUILD](../../bazel/helm/content_rules/BUILD#L2), [content_rules.py](../../bazel/helm/content_rules/content_rules.py#L3), [helm-template-test.sh](../../bazel/helm/helm-template-test.sh#L3) | [`//bazel/helm/content_rules:content_rules_test`](../../bazel/helm/content_rules/BUILD#L38) → [content_rules_test.py](../../bazel/helm/content_rules/content_rules_test.py#L2) |
