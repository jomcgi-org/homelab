load("@aspect_rules_py//py:defs.bzl", "py_library")

py_library(
    name = "projects",
    srcs = ["__init__.py"],
    visibility = ["//:__subpackages__"],
)

# The cluster enrollment roots intentionally have no package-local BUILD files.
# Expose only the two hand-maintained kustomizations the Renovate migration test
# needs, without turning either root into a Gazelle package.
filegroup(
    name = "renovate_enrollment_manifests",
    srcs = [
        "platform-gke/kustomization.yaml",
        "platform/kustomization.yaml",
    ],
    visibility = ["//projects/platform/renovate:__pkg__"],
)

exports_files(
    [
        "gke-apps/kustomization.yaml",
        "gke-apps/context-forge-gateway/application.yaml",
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
