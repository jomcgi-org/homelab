load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# files/ holds scripts the chart mounts from ConfigMaps; they are not Bazel
# targets, and a BUILD file there would take them out of :chart.
# gazelle:exclude files

filegroup(
    name = "deferred_adapter",
    srcs = [
        "files/itsaplan/client.ts",
        "files/itsaplan/creation-snapshot.mjs",
        "files/itsaplan/deferred.ts",
        "files/itsaplan/factory.ts",
        "files/itsaplan/transaction.ts",
    ],
    visibility = ["//projects/factory:__subpackages__"],
)

py_test(
    name = "deferred_test",
    srcs = ["deferred_test.py"],
    data = [
        "deferred.test.mjs",
        "files/itsaplan/deferred.ts",
        "files/itsaplan/transaction.ts",
        "@nodejs_linux_amd64//:node_bin",
    ],
    env = {"NODE_BIN": "$(rootpath @nodejs_linux_amd64//:node_bin)"},
    deps = ["@pip//pytest"],
)

# helm_chart's glob includes values-homelab.yaml as an optional values file.
# package_test checks it matches deploy/values.yaml until the Application
# switches to bundled values after a compatible release has published.
helm_chart(
    name = "chart",
    images = {
        "broker.image": "//projects/factory/broker/image:image.info",
        "codexTrial.image": "//projects/factory/codex-code-mode:image.info",
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
    name = "package_test",
    srcs = ["package_test.py"],
    data = [
        ":chart",
        ":chart.package",
        "//projects/factory/deploy:application.yaml",
        "//projects/factory/deploy:deploy_values",
        "//projects/factory/deploy:render_lock",
        # :chart carries the chart's Python tests, including the Node transaction suite.
        "//projects/platform-factory/substrate/resources:network-policy.yaml",
        "@multitool//tools/helm",
        "@nodejs_linux_amd64//:node_bin",
    ],
    env = {
        "APPLICATION": "$(rootpath //projects/factory/deploy:application.yaml)",
        "CHART_PACKAGE": "$(rootpath :chart.package)",
        "DEPLOY_VALUES": "$(rootpath //projects/factory/deploy:deploy_values)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "NODE_BIN": "$(rootpath @nodejs_linux_amd64//:node_bin)",
        "RENDER_LOCK": "$(rootpath //projects/factory/deploy:render_lock)",
        "SUBSTRATE_NETWORK_POLICY": "$(rootpath //projects/platform-factory/substrate/resources:network-policy.yaml)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
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
        "//projects/platform-factory/substrate/resources:network-policy.yaml",
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
        "SUBSTRATE_NETWORK_POLICY": "$(rootpath //projects/platform-factory/substrate/resources:network-policy.yaml)",
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
    name = "bootstrap_test",
    srcs = ["bootstrap_test.py"],
    data = ["files/itsaplan/bootstrap.py"],
    env = {
        "BOOTSTRAP": "$(rootpath files/itsaplan/bootstrap.py)",
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

py_test(
    name = "semgrep_sync_test",
    srcs = ["semgrep_sync_test.py"],
    data = ["files/semgrep_sync.py"],
    env = {"SYNC_SCRIPT": "$(rootpath files/semgrep_sync.py)"},
    deps = ["@pip//pytest"],
)
