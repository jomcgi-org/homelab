load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "nack",
    chart = "projects/platform/nack",
    chart_files = ":chart",
    namespace = "nats",
    release_name = "nack",
    # Upstream nack chart doesn't expose every securityContext knob; some
    # generated resources (CRDs, RBAC) trip these rules.
    semgrep_exclude_rules = [
        "require-resource-limits",
        "require-readiness-probe",
        "yaml.kubernetes.security.writable-filesystem-container.writable-filesystem-container",
        "yaml.kubernetes.security.run-as-non-root.run-as-non-root",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
