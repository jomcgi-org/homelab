load("//bazel/helm:defs.bzl", "helm_admissibility_render")

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/operators/oci-model-cache/helm/oci-model-cache-operator",
    chart_files = "//projects/operators/oci-model-cache/helm/oci-model-cache-operator:chart",
    namespace = "oci-model-cache",
    release_name = "oci-model-cache-operator",
    values_files = ["values.yaml"],
)
