# gazelle:argocd disabled
# gazelle:exclude Chart.yaml
# gazelle:exclude templates
# Negative-control chart for //bazel/helm:chart_admissibility_test. The helm
# gazelle extension would otherwise generate a helm_chart here and make this
# directory a package the parent cannot glob into.
exports_files(
    [
        "Chart.yaml",
        "templates/deployment.yaml",
    ],
    visibility = ["//bazel/helm:__pkg__"],
)
