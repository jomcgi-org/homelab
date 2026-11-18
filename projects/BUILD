load("@aspect_rules_py//py:defs.bzl", "py_library")

exports_files(
    ["platform-factory/network-policy/resources/argocd.yaml"],
    visibility = ["//projects/platform-factory/external-secrets/chart:__pkg__"],
)

filegroup(
    name = "backup_status_application",
    # application.yaml lives in the generated platform-gke package, which
    # exports it above. A glob cannot cross that package boundary, so depend
    # on the explicit label instead of globbing from this package.
    srcs = ["//projects/platform-gke/otel-collector:application.yaml"],
    visibility = ["//projects/platform/otel-collector:__pkg__"],
)

py_library(
    name = "projects",
    srcs = ["__init__.py"],
    visibility = ["//:__subpackages__"],
)

# The cluster enrollment roots intentionally have no package-local BUILD files.
# Expose the hub enrollment path for Renovate's deployment test without creating
# package-local BUILD files in those roots.
filegroup(
    name = "hub_renovate_enrollment_manifests",
    srcs = [
        "gke-cluster/hub-root-application.yaml",
        "gke-cluster/kustomization.yaml",
        "gke-cluster/root/root-application.yaml",
        "platform-gke/kustomization.yaml",
    ],
    visibility = ["//projects/platform/renovate:__pkg__"],
)

exports_files(
    [
        "gke-apps/kustomization.yaml",
    ],
    visibility = ["//projects/loom/deploy:__pkg__"],
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

# The faas workload test checks build_workload_spec against this CRD sample.
filegroup(
    name = "faas_workload_sample",
    srcs = ["embervm/crd/samples/workload-echo-fn.yaml"],
    visibility = ["//projects/monolith:__pkg__"],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

exports_files(
    ["factory-cluster/nodes/authentication-config.yaml"],
    visibility = ["//projects/platform-factory/kube-access/resources:__pkg__"],
)
