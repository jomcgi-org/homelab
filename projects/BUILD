load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

exports_files(
    ["platform-factory/kube-access/check-session-access.sh"],
    visibility = ["//projects/platform-factory/kube-access/resources:__pkg__"],
)

exports_files(
    [
        "platform/cloudnative-pg/deploy/values.yaml",
        "platform-factory/cloudnative-pg/values.yaml",
        "platform-factory/cloudnative-pg/resources/upstream/operator.yaml",
    ],
    visibility = ["//projects/platform-factory/cloudnative-pg/tests:__pkg__"],
)

py_library(
    name = "projects",
    srcs = ["__init__.py"],
    visibility = ["//:__subpackages__"],
)

# //bazel/helm/content_rules:coverage_test reads the factory cluster's
# Applications from this kustomization.
filegroup(
    name = "factory_kustomization",
    srcs = ["platform-factory/kustomization.yaml"],
    visibility = ["//bazel/helm/content_rules:__pkg__"],
)

# The factory deployment test follows the root Applications and platform tree.
filegroup(
    name = "factory_renovate_enrollment_manifests",
    srcs = [
        "factory-cluster/factory-root-application.yaml",
        "factory-cluster/kustomization.yaml",
        "factory-cluster/root/root-application.yaml",
        "platform-factory/kustomization.yaml",
    ],
    visibility = ["//projects/platform/renovate:__pkg__"],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ) + ["//projects/factory/browser-comparison:repo_docs"],
    visibility = ["//projects/monolith:__pkg__"],
)

sh_test(
    name = "substrate_ca_rotation_test",
    srcs = ["factory-cluster/rehearse-substrate-ca.sh"],
    args = ["$(rootpath @jq_toolchains//:resolved_toolchain)"],
    data = ["@jq_toolchains//:resolved_toolchain"],
)

exports_files(
    [
        "platform-factory/cert-manager/resources/network-policy.yaml",
        "platform-factory/cloudnative-pg/resources/network-policy.yaml",
        "platform-factory/kargo/resources/network-policy.yaml",
    ],
    visibility = ["//projects/platform-factory/network-policy/tests:__pkg__"],
)

exports_files(
    [
        "platform-factory/authentik/application.yaml",
        "platform-factory/cert-manager/application.yaml",
        "platform-factory/edge/application.yaml",
        "platform-factory/kargo/application.yaml",
    ],
    visibility = ["//projects/platform-factory/image-audit:__pkg__"],
)
