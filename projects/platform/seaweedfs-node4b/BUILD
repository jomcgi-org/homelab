load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "seaweedfs-node4b",
    chart = "projects/platform/seaweedfs-node4b",
    chart_files = ":chart",
    namespace = "seaweedfs",
    release_name = "seaweedfs-node4b",
    # Upstream doesn't expose securityContext via wrapper values
    semgrep_exclude_rules = [
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
