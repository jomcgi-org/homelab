load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")

# gazelle:exclude **/*.bzl
load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("@multitool//:tools.bzl", MULTITOOL_TOOLS = "TOOLS")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test")

exports_files([
    "argocd-live-diff.sh",
    "chart-admissibility-test.sh",
    "chart-version.sh",
    "check-missed-bump.sh",
    "helm-assert-annotations.sh",
    "helm-template-test.sh",
    "lint.sh",
    "push.sh.tpl",
    "render.sh",
    "render-manifests.sh",
    "verify-published-images.sh",
    "write-back-versions.sh",
])

MANIFEST_SETS = [
    # First-party applications rendered with their deploy values.
    "//projects/embervm/deploy:manifests/all.yaml",
    "//projects/embervm/deploy:manifests_gke/all.yaml",
    "//projects/embervm/dev/deploy:manifests/all.yaml",
    "//projects/inference/deploy:manifests/all.yaml",
    "//projects/inference/deploy:manifests_gke/all.yaml",
    "//projects/mcp/context-forge-gateway/deploy:manifests/all.yaml",
    "//projects/mcp/context-forge-gateway/deploy:manifests_gke/all.yaml",
    "//projects/monolith-agents/deploy:manifests/all.yaml",
    "//projects/monolith-agents/deploy:manifests_gke/all.yaml",
    "//projects/monolith-public/deploy:manifests/all.yaml",
    "//projects/monolith-public/deploy:manifests_gke/all.yaml",
    "//projects/monolith/deploy:manifests/all.yaml",
    "//projects/monolith/deploy:manifests_gke/all.yaml",
    "//projects/monolith/dev/deploy:manifests/all.yaml",
    "//projects/operators/oci-model-cache/deploy:manifests/all.yaml",
    # Platform applications rendered by argocd_app.
    "//projects/platform-gke/tailscale:manifests/all.yaml",
    "//projects/platform/argo-workflows:manifests/all.yaml",
    "//projects/platform/argo-workflows:manifests_gke/all.yaml",
    "//projects/platform/argocd:manifests/all.yaml",
    "//projects/platform/argocd:manifests_gke/all.yaml",
    "//projects/platform/authentik:manifests/all.yaml",
    "//projects/platform/authentik:manifests_gke/all.yaml",
    "//projects/platform/cert-manager:manifests/all.yaml",
    "//projects/platform/cert-manager:manifests_gke/all.yaml",
    "//projects/platform/cilium:manifests/all.yaml",
    "//projects/platform/cloudflare-gateway:manifests/all.yaml",
    "//projects/platform/cloudflare-gateway:manifests_gke/all.yaml",
    "//projects/platform/coredns:manifests/all.yaml",
    "//projects/platform/kargo:manifests/all.yaml",
    "//projects/platform/keda:manifests/all.yaml",
    "//projects/platform/kyverno:manifests/all.yaml",
    "//projects/platform/longhorn:manifests/all.yaml",
    "//projects/platform/nvidia-gpu-operator:manifests/all.yaml",
    "//projects/platform/onepassword:manifests/all.yaml",
    "//projects/platform/opentelemetry-operator:manifests/all.yaml",
    "//projects/platform/otel-collector:manifests/all.yaml",
    "//projects/platform/otel-collector:manifests_gke/all.yaml",
    "//projects/platform/polylane:manifests/all.yaml",
    "//projects/platform/priority-classes:manifests/all.yaml",
    "//projects/platform/renovate:manifests/all.yaml",
    "//projects/platform/spire:manifests/all.yaml",
    "//projects/platform/spire:manifests_gke/all.yaml",
]

# Documentation-only record of explicit render skips. Entries stay out of
# MANIFEST_SETS only for the stated reason, so a chart cannot disappear from
# the gate without leaving reviewable evidence here. The completeness test
# below enforces the record against the source tree.
SKIPPED_MANIFEST_SETS = {
    "projects/embervm/dev/deploy/values-store-validation-gke.yaml": "Default-off store validation overlay, rendered by its hermetic config test.",
    "projects/embervm/dev/deploy/values-recovery-gke.yaml": "Recovery overlay, not rendered by CI.",
    "projects/monolith/dev/deploy/values-recovery-gke.yaml": "Recovery overlay, not rendered by CI.",
    "projects/platform/atlas-operator/deploy/values.yaml": "Remote chart with no local Chart.yaml or render target.",
    "projects/platform/cf-ingress-library": "Library chart, which renders nothing.",
    "projects/platform/cloudnative-pg/deploy/values.yaml": "Remote chart with no local Chart.yaml or render target.",
    "projects/shared/helm/homelab-library": "Library chart, which renders nothing.",
}

# Keep the four application charts free of inert custom policies in every
# deployed values stack, including the historical home/dev render targets.
NO_CILIUM_MANIFESTS = [
    "//projects/%s/deploy:manifests%s/all.yaml" % (project, suffix)
    for project in [
        "monolith",
        "monolith-public",
        "monolith-agents",
        "embervm",
    ]
    for suffix in [
        "",
        "_gke",
    ]
] + [
    "//projects/monolith/dev/deploy:manifests/all.yaml",
    "//projects/embervm/dev/deploy:manifests/all.yaml",
]

sh_test(
    name = "no_cilium_policies_test",
    srcs = ["no-cilium-policies-test.sh"],
    args = ["$(rootpath %s)" % manifest for manifest in NO_CILIUM_MANIFESTS],
    data = NO_CILIUM_MANIFESTS,
)

# Most CRD definitions are present in the rendered charts and are converted at
# test time. These operators are installed from remote charts, or omit CRDs
# from their wrapper chart, so their version-pinned definitions are additional
# hermetic inputs.
EXTERNAL_CRD_SOURCES = [
    "@atlasmigration_crd_0_7_28//file",
    "@cilium_network_policy_crd_1_20_0//file",
    "@cloudnative_pg_cluster_crd_1_30_0//file",
    "@cloudnative_pg_database_crd_1_30_0//file",
    "@cloudnative_pg_scheduled_backup_crd_1_30_0//file",
]

py_venv_binary(
    name = "crd_schemas",
    srcs = ["crd-schemas.py"],
    main = "crd-schemas.py",
    visibility = ["//:__subpackages__"],
    deps = ["@pip//pyyaml"],
)

sh_test(
    name = "chart_admissibility_test",
    size = "large",
    timeout = "long",
    srcs = ["chart-admissibility-test.sh"],
    args = [
        "$(rootpath @multitool//tools/helm)",
        "$(rootpath @kubeconform_0_8_0_linux_amd64//:binary)",
        "$(rootpath :crd_schemas)",
        "$(rootpath //bazel/helm/testdata/bad-secretkeyref:Chart.yaml)",
        "$(rootpath @kubernetes_json_schema_1_36_3//:secret-v1.json)",
    ] + ["$(rootpath {})".format(source) for source in EXTERNAL_CRD_SOURCES] + ["--"] + [
        "$(rootpath {})".format(manifest)
        for manifest in MANIFEST_SETS
    ],
    data = [
        ":crd_schemas",
        "//bazel/helm/testdata/bad-secretkeyref:Chart.yaml",
        "//bazel/helm/testdata/bad-secretkeyref:templates/deployment.yaml",
        "@kubeconform_0_8_0_linux_amd64//:binary",
        "@kubernetes_json_schema_1_36_3//:schemas",
        "@kubernetes_json_schema_1_36_3//:secret-v1.json",
        "@multitool//tools/helm",
    ] + EXTERNAL_CRD_SOURCES + MANIFEST_SETS,
)

sh_test(
    name = "chart_admissibility_completeness_test",
    srcs = ["chart-admissibility-completeness-test.sh"],
    args = ["$(rootpath chart-admissibility-completeness-test.sh)"],
    data = ["BUILD"],
    tags = ["local"],
)

sh_test(
    name = "chart_admissibility_completeness_regression_test",
    srcs = ["chart-admissibility-completeness_test.sh"],
    data = [":chart-admissibility-completeness-test.sh"],
)

sh_test(
    name = "argocd_live_diff_test",
    srcs = ["argocd-live-diff_test.sh"],
    data = [":argocd-live-diff.sh"],
)

sh_test(
    name = "check_missed_bump_test",
    srcs = ["check-missed-bump_test.sh"],
    data = [":check-missed-bump.sh"],
)

sh_test(
    name = "standalone_lint_test",
    srcs = ["lint_test.sh"],
    data = [":lint.sh"],
)

sh_test(
    name = "standalone_render_test",
    srcs = ["render_test.sh"],
    data = [":render.sh"],
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

sh_test(
    name = "verify_published_images_test",
    srcs = ["verify-published-images_test.sh"],
    data = [":verify-published-images.sh"],
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

semgrep_target_test(
    name = "crd_schemas_semgrep_test",
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":crd_schemas",
)

py_library(
    name = "helm",
    srcs = ["crd-schemas.py"],
    visibility = ["//:__subpackages__"],
    deps = ["@pip//pyyaml"],
)
