load("//bazel/helm:defs.bzl", "helm_admissibility_render")

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/platform-factory/substrate/egress-scan/chart",
    chart_files = "//projects/platform-factory/substrate/egress-scan/chart:chart",
    namespace = "ate-system",
    release_name = "egress-scan",
    values_files = ["//projects/platform-factory/substrate/egress-scan/chart:image_values"],
)

exports_files(["application.yaml"])
