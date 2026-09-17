load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    # The vendored upstream subchart is linted by its own repo, not ours.
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "onepassword",
    chart = "projects/platform/onepassword",
    chart_files = ":chart",
    # The Application lives in projects/platform-gke/onepassword, so the render
    # genrule has nothing here to render; the semgrep scan reads the chart, not
    # the Application.
    generate_manifests = False,
    namespace = "default",
    release_name = "connect",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
