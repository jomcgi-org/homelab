load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    images = {
        "image": "//projects/scotland-connector/image:image.info",
    },
    lint = False,
    publish = True,
    visibility = [
        "//projects/scotland-connector:__subpackages__",  # keep
        "//visibility:public",
    ],
)

# Kept here, not generated from deploy/application.yaml, because gazelle
# resets chart visibility otherwise (the monolith-public pattern).
argocd_app(
    name = "scotland-connector",
    chart = "projects/scotland-connector/chart",
    chart_files = ":chart",
    generate_manifests = False,
    namespace = "scotland-connector",
    release_name = "scotland-connector",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/scotland-connector/deploy:deploy_values"],
)

py_test(
    name = "chart_posture_test",
    srcs = ["chart_posture_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "//projects/monolith-public/chart:chart_values",
        "//projects/scotland-connector/deploy:deploy_values",
        "@multitool//tools/helm",
    ] + glob(["templates/**/*"]),
    env = {
        "DEPLOY_VALUES": "$(rootpath //projects/scotland-connector/deploy:deploy_values)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "MONOLITH_PUBLIC_VALUES": "$(rootpath //projects/monolith-public/chart:chart_values)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
