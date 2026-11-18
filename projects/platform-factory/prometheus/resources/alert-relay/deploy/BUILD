load("//bazel/helm:defs.bzl", "argocd_app", "helm_admissibility_render")

argocd_app(
    name = "alert-relay",
    chart = "projects/platform-factory/prometheus/resources/alert-relay/chart",
    chart_files = "//projects/platform-factory/prometheus/resources/alert-relay/chart:chart",
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "prometheus",
    release_name = "alert-relay",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform-factory/prometheus/resources/alert-relay/chart:values.yaml",
        "//projects/platform-factory/prometheus/resources/alert-relay/chart:image_values",
    ],  # keep
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/platform-factory/prometheus/resources/alert-relay/chart",
    chart_files = "//projects/platform-factory/prometheus/resources/alert-relay/chart:chart",
    namespace = "prometheus",
    release_name = "alert-relay",
    values_files = ["//projects/platform-factory/prometheus/resources/alert-relay/chart:image_values"],
)

exports_files(["application.yaml"])
