# The chart package owns hub rendering and its deployment contract test.
# Keep this package as data-only so Gazelle does not create a duplicate render.
# gazelle:argocd disabled
filegroup(
    name = "manifests",
    srcs = [
        "application.yaml",
        "kustomization.yaml",
    ],
    visibility = ["//projects/platform/renovate:__pkg__"],
)
