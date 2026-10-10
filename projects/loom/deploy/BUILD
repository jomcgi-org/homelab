load("//bazel/tools/pytest:defs.bzl", "py_test")

MANIFESTS = [
    "cnpg-cluster.yaml",
    "cnpg-metrics-service.yaml",
    "cnpg-scheduledbackup.yaml",
    "httproute.yaml",
    "image-pull-secret.yaml",
    "namespace.yaml",
    "onepassworditem-cnpg-backup-gcs.yaml",
    "onepassworditem-r2.yaml",
]

exports_files(
    MANIFESTS + ["values.yaml"],
    visibility = ["//bazel/helm:__pkg__"],
)

py_test(
    name = "deploy_manifests_test",
    srcs = ["deploy_manifests_test.py"],
    data = MANIFESTS + [
        "README.md",
        "kustomization.yaml",
        "values.yaml",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

genrule(
    name = "manifests",
    srcs = [
        "values.yaml",
        "@loom_chart_0_2_0//:Chart.yaml",
        "@loom_chart_0_2_0//:chart_files",
    ],
    outs = ["manifests/all.yaml"],
    cmd = "$(location @multitool//tools/helm) template loom " +
          "$$(dirname $(location @loom_chart_0_2_0//:Chart.yaml)) " +
          "--namespace loom --include-crds --values $(location values.yaml) > $@",
    local = True,
    tags = ["manual"],
    tools = ["@multitool//tools/helm"],
    visibility = ["//bazel/helm:__pkg__"],
)

py_test(
    name = "chart_render_test",
    srcs = ["chart_render_test.py"],
    data = MANIFESTS + [
        "values.yaml",
        "//projects/gke-apps/loom:application.yaml",
        "//projects/platform/otel-collector:loom_monitoring_values",
        "@loom_chart_0_2_0//:Chart.yaml",
        "@loom_chart_0_2_0//:chart_files",
        "@multitool//tools/helm",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "LOOM_CHART_YAML": "$(rootpath @loom_chart_0_2_0//:Chart.yaml)",
        "LOOM_APPLICATION": "$(rootpath //projects/gke-apps/loom:application.yaml)",
        "LOOM_MONITORING_VALUES": "$(rootpath //projects/platform/otel-collector:loom_monitoring_values)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "application_test",
    srcs = ["application_test.py"],
    data = MANIFESTS + [
        "kustomization.yaml",
        "values.yaml",
        "//projects:gke-apps/kustomization.yaml",
        "//projects/gke-apps/context-forge-gateway:application.yaml",
        "//projects/gke-apps/loom:application.yaml",
        "//projects/gke-apps/loom:argocd-repo-cred.yaml",
        "//projects/gke-apps/loom:kustomization.yaml",
    ],
    env = {
        "LOOM_APPLICATION": "$(rootpath //projects/gke-apps/loom:application.yaml)",
        "HUB_KUSTOMIZATION": "$(rootpath //projects:gke-apps/kustomization.yaml)",
        "GATEWAY_APPLICATION": "$(rootpath //projects/gke-apps/context-forge-gateway:application.yaml)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

# Markdown read by the monolith doc-manifest genrules (#6446).
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
