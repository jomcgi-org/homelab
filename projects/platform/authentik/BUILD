load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "authentik",
    chart = "projects/platform/authentik",
    chart_files = ":chart",
    namespace = "authentik",
    release_name = "authentik",
    semgrep_exclude_rules = [
        "require-resource-limits",
        "require-readiness-probe",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = ["values.yaml"],
)
