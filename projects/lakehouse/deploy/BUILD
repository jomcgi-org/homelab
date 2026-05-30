load("//bazel/helm:defs.bzl", "argocd_app")

# gazelle:argocd disabled
#
# Hand-maintained (see ../chart/BUILD). Gazelle's helm extension would regenerate
# this argocd_app without our semgrep_exclude_rules and would not fix the chart's
# cross-package visibility, so we keep both BUILDs out of gazelle's hands.
#
# Path-based ArgoCD Application for the event-sourced lakehouse worker/quack/
# dispatcher Deployments + KEDA scalers. Hand-written (not left to gazelle) so we
# can carry semgrep_exclude_rules — gazelle's helm extension manages only chart/
# chart_files/release_name/namespace/values_files/tags and preserves this attr.
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
