# gazelle:exclude **/*.bzl
load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("@multitool//:tools.bzl", MULTITOOL_TOOLS = "TOOLS")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

exports_files([
    "chart-version.sh",
    "check-missed-bump.sh",
    "helm-assert-annotations.sh",
    "helm-template-test.sh",
    "push.sh.tpl",
    "render-manifests.sh",
    "write-back-versions.sh",
])

sh_test(
    name = "check_missed_bump_test",
    srcs = ["check-missed-bump_test.sh"],
    data = [":check-missed-bump.sh"],
)

sh_test(
    name = "chart_version_test",
    srcs = ["chart-version_test.sh"],
    data = [":chart-version.sh"],
)

sh_test(
    name = "write_back_versions_test",
    srcs = ["write-back-versions_test.sh"],
    data = [":write-back-versions.sh"],
)

sh_test(
    name = "push_test",
    srcs = ["push_test.sh"],
    data = [":push.sh.tpl"],
)

# Every chart's rendered manifests, validated against Kubernetes API schemas
# by admissibility-test.sh (#4831). Platform charts render via argocd_app;
# first-party applications render via helm_admissibility_render. A chart is
# added here by adding it to one of those two, never by hand-editing this
# list. Library charts (cf-ingress-library, signoz-dashboards-library,
# homelab-library) render no objects and are excluded; cloudnative-pg and
# atlas-operator pull upstream charts by repoURL and have no vendored chart
# to validate.
MANIFEST_SETS = [
    # First-party applications.
    "//projects/embervm/deploy:manifests/all.yaml",
    "//projects/embervm/dev/deploy:manifests/all.yaml",
    "//projects/monolith/deploy:manifests/all.yaml",
    "//projects/monolith/dev/deploy:manifests/all.yaml",
    "//projects/monolith-public/deploy:manifests/all.yaml",
    "//projects/inference/deploy:manifests/all.yaml",
    "//projects/mcp/context-forge-gateway/deploy:manifests/all.yaml",
    "//projects/operators/oci-model-cache/deploy:manifests/all.yaml",
    # Platform charts.
    "//projects/platform/argo-workflows:manifests/all.yaml",
    "//projects/platform/argocd:manifests/all.yaml",
    "//projects/platform/authentik:manifests/all.yaml",
    "//projects/platform/cert-manager:manifests/all.yaml",
    "//projects/platform/cilium:manifests/all.yaml",
    "//projects/platform/coredns:manifests/all.yaml",
    "//projects/platform/kargo:manifests/all.yaml",
    "//projects/platform/keda:manifests/all.yaml",
    "//projects/platform/kyverno:manifests/all.yaml",
    "//projects/platform/longhorn:manifests/all.yaml",
    "//projects/platform/nvidia-gpu-operator:manifests/all.yaml",
    "//projects/platform/opentelemetry-operator:manifests/all.yaml",
    "//projects/platform/priority-classes:manifests/all.yaml",
    "//projects/platform/renovate:manifests/all.yaml",
    "//projects/platform/seaweedfs:manifests/all.yaml",
    "//projects/platform/seaweedfs-node4:manifests/all.yaml",
    "//projects/platform/seaweedfs-node4b:manifests/all.yaml",
    "//projects/platform/signoz:manifests/all.yaml",
    "//projects/platform/signoz-addons/alerts:manifests/all.yaml",
    "//projects/platform/signoz-addons/dashboard-sidecar:manifests/all.yaml",
]

sh_test(
    name = "chart_admissibility_test",
    srcs = ["admissibility-test.sh"],
    # kubeconform downloads JSON schemas over the network on cache miss.
    tags = ["requires-network"],
    args = [
        "$(rootpath @multitool//tools/helm)",
        "$(rootpath @multitool//tools/kubeconform)",
        "$(rootpath //bazel/helm:testdata/bad-secretkeyref/Chart.yaml)",
    ] + ["$(rootpath {})".format(m) for m in MANIFEST_SETS],
    data = [
        "@multitool//tools/helm",
        "@multitool//tools/kubeconform",
        "//bazel/helm:testdata/bad-secretkeyref/Chart.yaml",
    ] + glob(["testdata/bad-secretkeyref/templates/**"]) + MANIFEST_SETS,
    timeout = "long",
)

sh_binary(
    name = "validate_manifests",
    srcs = ["ci-validate-manifests.sh"],
    data = [MULTITOOL_TOOLS["helm"]],
    env = {
        "HELM": "$(rootpath {})".format(MULTITOOL_TOOLS["helm"]),
    },
)

sh_binary(
    name = "render_all",
    srcs = ["render-all-manifests.sh"],
    data = [MULTITOOL_TOOLS["helm"]],
    env = {
        "HELM": "$(rootpath {})".format(MULTITOOL_TOOLS["helm"]),
    },
    visibility = ["//bazel/tools/format:__pkg__"],
)

# keep: Gazelle's bzl extension mis-resolves @rules_shell//shell:sh_test.bzl
bzl_library(
    name = "defs",
    srcs = [
        "admissibility.bzl",  # keep
        "app.bzl",  # keep
        "chart.bzl",  # keep
        "defs.bzl",  # keep
        "images.bzl",  # keep
        "push.bzl",  # keep
        "render.bzl",  # keep
        "test.bzl",  # keep
    ],
    visibility = ["//visibility:public"],
    deps = [
        "//bazel/semgrep/defs",  # keep
        "//bazel/tools/oci:providers",  # keep
        "@bazel_skylib//lib:paths",  # keep
        "@rules_shell//shell:rules_bzl",  # keep
    ],
)
