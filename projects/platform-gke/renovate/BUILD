# The chart package owns GKE rendering and the focused migration contract test.
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
