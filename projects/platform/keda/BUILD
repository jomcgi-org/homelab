load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "keda",
    chart = "projects/platform/keda",
    chart_files = ":chart",
    namespace = "keda",
    release_name = "keda",
    # KEDA is secure-by-default (runAsNonRoot, readOnlyRootFilesystem, drops ALL
    # caps, allowPrivilegeEscalation: false, seccomp RuntimeDefault). The two
    # Semgrep Pro security rules below false-positive on the embedded `containers:`
    # schema text inside the bundled KEDA CRDs (the CRD `description:` fields), not
    # on the actual workload containers — same false positive nats/seaweedfs exclude.
    # require-tmp-emptydir: KEDA's read-only-rootfs containers do not mount a /tmp
    # emptyDir and the upstream chart exposes no wrapper value to add one.
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
