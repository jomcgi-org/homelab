load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    images = {
        "broker.image": "//projects/factory/broker/image:image.info",
    },
    # The vendored kagent subcharts are linted by their own repository.
    lint = False,
    publish = True,
    visibility = [
        "//projects/factory:__subpackages__",  # keep
        "//visibility:public",
    ],
)

# Kept here, not generated from deploy/application.yaml, because gazelle
# resets chart visibility otherwise (the monolith-public pattern).
argocd_app(
    name = "factory",
    chart = "projects/factory/chart",
    chart_files = ":chart",
    generate_manifests = False,
    namespace = "kagent",
    release_name = "factory",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/factory/deploy:deploy_values"],
)

py_test(
    name = "chart_test",
    srcs = ["chart_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.schema.json",
        "values.yaml",
        "//projects/factory/deploy:deploy_values",
        "@multitool//tools/helm",
    ] + glob([
        "charts/**",
        "crds/**",
        "templates/**",
    ]),
    env = {
        "DEPLOY_VALUES": "$(rootpath //projects/factory/deploy:deploy_values)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
