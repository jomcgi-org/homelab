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
    # Cilium is the CNI datapath: the agent legitimately runs privileged with a
    # writable rootfs, hostPath mounts, and root, and does not expose upstream
    # wrapper values to add /tmp emptyDirs or resource limits everywhere. The Pro
    # k8s security rules also false-positive on the schema text inside Cilium's
    # bundled CRDs (same pattern as longhorn/keda). Exclusions mirror longhorn.
    semgrep_exclude_rules = [
        "no-privileged",
        "require-resource-limits",
        "require-readiness-probe",
        "require-tmp-emptydir",
        "yaml.kubernetes.security.privileged-container.privileged-container",
        "yaml.kubernetes.security.allow-privilege-escalation.allow-privilege-escalation",
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
