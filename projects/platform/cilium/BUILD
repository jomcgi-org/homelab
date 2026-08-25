load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

# Read by //projects/platform/signoz:hubble_monolith_policy_drops_test (#4659):
# the hubble drop metric's context labels live in these two files, and the test
# pins them in step with the alert that consumes the labels.
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
