load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "temporal",
    chart = "projects/platform/temporal",
    chart_files = ":chart",
    namespace = "temporal",
    release_name = "temporal",
    # Upstream temporal chart: securityContext not exposed for every container;
    # resource limits not configurable for all subcomponents; some containers
    # lack readiness probes; schema setup uses a writable filesystem.
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
