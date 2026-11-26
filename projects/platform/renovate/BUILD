load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    visibility = ["//visibility:public"],
)

exports_files(
    ["values-factory.yaml"],
    visibility = ["//projects/platform-factory/renovate:__pkg__"],
)

argocd_app(
    name = "renovate",
    chart = "projects/platform/renovate",
    chart_files = ":chart",
    namespace = "monolith-workflows",
    release_name = "renovate",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)

argocd_app(
    name = "renovate-gke",
    chart = "projects/platform/renovate",
    chart_files = ":chart",
    namespace = "monolith-workflows",
    release_name = "renovate",
    tags = [
        "helm",
        "template",
    ],
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        "values-gke.yaml",
    ],
)

sh_test(
    name = "config_validator_test",
    srcs = ["config_validator_test.sh"],
    # Invoke the packaged renovate-config-validator CLI directly. The image's
    # shell wrapper also initializes containerbase tools in /tmp.
    args = [
        "$(rootpath //:renovate.json)",
        "/usr/local/renovate/node",
        "--use-openssl-ca",
        "/usr/local/renovate/dist/config-validator.js",
    ],
    data = ["//:renovate.json"],
    exec_properties = {
        "test.container-image": "docker://ghcr.io/renovatebot/renovate:44.14.10",
        # The image entrypoint finds containerbase in /usr/local/sbin.
        "test.env-overrides": "PATH=/bin:/usr/bin:/usr/local/bin:/usr/local/sbin",
        # The Renovate image runs as UID 12021 and writes fixtures to TEST_TMPDIR.
        "test.nonroot-workspace": "true",
    },
)

# Policy fixtures include the rendered maintenance assets and real Node publisher.
py_test(
    name = "dependency_policy_test",
    srcs = ["dependency_policy_test.py"],
    args = ["$(rootpath dependency_policy_test.py)"],
    data = [
        ":chart",
        "//:renovate.json",
        "@multitool//tools/helm",
        "@nodejs_linux_amd64//:node_bin",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "NODE_BIN": "$(rootpath @nodejs_linux_amd64//:node_bin)",
    },
    deps = [
        "@pip//packaging",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

# The factory root must deploy only a CronJob with the shared runtime limits.
py_test(
    name = "factory_deployment_test",
    srcs = ["factory_deployment_test.py"],
    args = ["$(rootpath factory_deployment_test.py)"],
    data = [
        ":chart",
        "//projects:factory_renovate_enrollment_manifests",
        "//projects/platform-factory/renovate:manifests",
        "@multitool//tools/helm",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = ["@pip//pyyaml"],
)
