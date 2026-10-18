load("@aspect_rules_py//py:defs.bzl", "py_library")

filegroup(
    name = "backup_status_application",
    # CI tests before Gazelle generates temporary enrollment packages. A glob
    # follows those package boundaries during the later formatting checks.
    srcs = glob(
        ["platform-gke/otel-collector/application.yaml"],
        allow_empty = True,
    ),
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
