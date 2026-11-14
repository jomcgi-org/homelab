load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# files/ holds scripts the chart mounts from ConfigMaps; they are not Bazel
# targets, and a BUILD file there would take them out of :chart.
# gazelle:exclude files

helm_chart(
    name = "chart",
    images = {
        "broker.image": "//projects/factory/broker/image:image.info",
        "connector.image": "//projects/factory/connector/image:image.info",
        "reviewer.image": "//projects/factory/reviewer/image:image.info",
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
        "files/**",
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

py_test(
    name = "itsaplan_test",
    srcs = ["itsaplan_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.schema.json",
        "values.yaml",
        "//projects/factory/deploy:application.yaml",
        "//projects/factory/deploy:deploy_values",
        "@multitool//tools/helm",
    ] + glob([
        "charts/**",
        "crds/**",
        "files/**",
        "templates/**",
    ]),
    env = {
        "APPLICATION": "$(rootpath //projects/factory/deploy:application.yaml)",
        "DEPLOY_VALUES": "$(rootpath //projects/factory/deploy:deploy_values)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "secret_init_test",
    srcs = ["secret_init_test.py"],
    data = ["files/itsaplan/secret_init.py"],
    env = {
        "SECRET_INIT": "$(rootpath files/itsaplan/secret_init.py)",
    },
    deps = ["@pip//pytest"],
)

py_test(
    name = "connector_test",
    srcs = ["connector_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.schema.json",
        "values.yaml",
        "//projects/factory/deploy:deploy_values",
        "//projects/factory/deploy:render_lock",
        "@multitool//tools/helm",
    ] + glob([
        "charts/**",
        "crds/**",
        "files/**",
        "templates/**",
    ]),
    env = {
        "DEPLOY_VALUES": "$(rootpath //projects/factory/deploy:deploy_values)",
        "RENDER_LOCK": "$(rootpath //projects/factory/deploy:render_lock)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "reviewer_test",
    srcs = ["reviewer_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.schema.json",
        "values.yaml",
        "//projects/factory/chart/configcheck",
        "//projects/factory/deploy:deploy_values",
        "//projects/factory/deploy:render_lock",
        "@multitool//tools/helm",
    ] + glob([
        "charts/**",
        "crds/**",
        "files/**",
        "templates/**",
    ]),
    env = {
        "CONFIG_CHECK_BIN": "$(rootpath //projects/factory/chart/configcheck)",
        "DEPLOY_VALUES": "$(rootpath //projects/factory/deploy:deploy_values)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "RENDER_LOCK": "$(rootpath //projects/factory/deploy:render_lock)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
