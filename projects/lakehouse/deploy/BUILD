load("//bazel/helm:defs.bzl", "argocd_app")

# gazelle:argocd disabled
#
# Hand-maintained (see ../chart/BUILD). Gazelle's helm extension would regenerate
# this argocd_app without our semgrep_exclude_rules and would not fix the chart's
# cross-package visibility, so we keep both BUILDs out of gazelle's hands.
#
# OCI-sourced ArgoCD Application for the event-sourced lakehouse worker/quack/
# dispatcher Deployments + KEDA scalers (application.yaml pulls the chart from
# ghcr.io/jomcgi/homelab/charts by version). This argocd_app target only renders
# the LOCAL chart for CI validation (helm_template_test / semgrep_manifest_test) —
# chart/chart_files/values_files stay path-local regardless of how the deployed
# Application sources the chart. Hand-written (not left to gazelle) so we can carry
# semgrep_exclude_rules — gazelle's helm extension manages only chart/chart_files/
# release_name/namespace/values_files/tags and preserves this attr.
#
# require-readiness-probe: the gap-drain / iceberg-builder / housekeeping worker
# pods and the dispatcher pods are Temporal task-queue pollers / NATS consumers
# with NO inbound HTTP serving path, so an HTTP readinessProbe is meaningless for
# them (Temporal/NATS gate their own work intake). The quack pods DO carry an
# HTTP /healthz readinessProbe. Same rationale nats/keda use to exclude this rule
# for non-HTTP workloads.
argocd_app(
    name = "lakehouse",
    chart = "projects/lakehouse/chart",
    chart_files = "//projects/lakehouse/chart:chart",
    namespace = "lakehouse",
    release_name = "lakehouse",
    semgrep_exclude_rules = [
        "require-readiness-probe",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/lakehouse/chart:values.yaml",
        "values.yaml",
    ],
)
