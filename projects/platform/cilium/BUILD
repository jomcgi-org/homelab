load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

# The Hubble drop metric's context labels are exported for downstream checks.
exports_files([
    "values.yaml",
    "bootstrap/cilium-helmchart.yaml",
])

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "cilium",
    chart = "projects/platform/cilium",
    chart_files = ":chart",
    namespace = "kube-system",
    release_name = "cilium",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
