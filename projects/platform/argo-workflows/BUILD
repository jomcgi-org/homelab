load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "argo-workflows",
    chart = "projects/platform/argo-workflows",
    chart_files = ":chart",
    namespace = "monolith-workflows",
    release_name = "argo-workflows",
    # The bundled Argo CRDs (workflows, workflowtemplates, ...) embed full
    # pod/container OpenAPI schemas inside their `description:` text. Two Semgrep
    # Pro rules match that embedded schema text, not real workload containers -
    # the same false positive keda/nats/seaweedfs exclude.
    # - writable-filesystem-container also matches the argo-server container,
    #   which ships readOnlyRootFilesystem:false upstream (it writes /tmp) and the
    #   chart exposes no wrapper value to flip it; internal-only UI, accepted.
    # - require-tmp-emptydir: the read-only controller container has no /tmp
    #   emptyDir and the chart offers no wrapper value to add one.
    semgrep_exclude_rules = [
        "yaml.kubernetes.security.writable-filesystem-container.writable-filesystem-container",
        "yaml.kubernetes.security.allow-privilege-escalation.allow-privilege-escalation",
        "require-tmp-emptydir",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)

argocd_app(
    name = "argo-workflows-gke",
    chart = "projects/platform/argo-workflows",
    chart_files = ":chart",
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "monolith-workflows",
    release_name = "argo-workflows",
    tags = [
        "helm",
        "template",
    ],
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        "values-gke.yaml",
    ],
)
