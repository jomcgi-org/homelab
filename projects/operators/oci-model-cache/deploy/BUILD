load("//bazel/helm:defs.bzl", "helm_admissibility_render")

# Rendered for //bazel/helm:chart_admissibility_test (#4831). Release name and
# namespace mirror deploy/application.yaml. The Application pulls the published
# OCI chart; rendering the vendored source chart with the same values overlay
# validates the same objects ArgoCD will sync.
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/operators/oci-model-cache/helm/oci-model-cache-operator",
    chart_files = "//projects/operators/oci-model-cache/helm/oci-model-cache-operator:chart",
    release_name = "oci-model-cache-operator",
    namespace = "oci-model-cache",
    values_files = ["values.yaml"],
)
