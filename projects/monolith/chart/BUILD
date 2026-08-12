load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

filegroup(
    name = "migrations",
    srcs = glob([
        "migrations/*.sql",
        "migrations/atlas.sum",
    ]),
    visibility = ["//projects/monolith:__subpackages__"],
)

sh_test(
    name = "atlas_sum_test",
    srcs = ["//bazel/tools/format:validate-atlas-sum.sh"],
    data = [
        ":migrations",
        "@multitool//tools/atlas",
    ],
    env = {
        "ATLAS_BIN": "$(rootpath @multitool//tools/atlas)",
        "MIGRATIONS_DIR": "projects/monolith/chart/migrations",
    },
)

helm_chart(
    name = "chart",
    images = {
        "backend.image": "//projects/monolith:image.info",
        "frontend.image": "//projects/monolith/frontend:image.info",
        "jobs.image": "//projects/monolith:jobs_image.info",
        "progress.image": "//projects/monolith:progress_image.info",
        "whatsapp.image": "//projects/monolith/whatsapp:image.info",
    },
    lint = False,
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)

py_test(
    name = "chart_env_identity_test",
    srcs = ["chart_env_identity_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.yaml",
        "//projects/monolith:module_configs",
        "//projects/monolith/deploy:application",
        "//projects/monolith/deploy:deploy_values",
        "//projects/monolith/dev/deploy:application",
        "//projects/monolith/dev/deploy:deploy_values",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "charts/**/*",
        "migrations/**/*",
    ]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "DEPLOY_VALUES": "$(rootpath //projects/monolith/deploy:deploy_values)",
        "DEV_VALUES": "$(rootpath //projects/monolith/dev/deploy:deploy_values)",
        # The Applications, so releaseName is read from where it is DECLARED.
        "PROD_APPLICATION": "$(rootpath //projects/monolith/deploy:application)",
        "DEV_APPLICATION": "$(rootpath //projects/monolith/dev/deploy:application)",
    },
    deps = ["@pip//pytest"],
)

py_test(
    name = "chart_env_vars_test",
    srcs = ["chart_env_vars_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.yaml",
        "//projects/monolith:module_configs",
        "//projects/monolith/deploy:deploy_values",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "charts/**/*",
        "migrations/**/*",
    ]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "DEPLOY_VALUES": "$(rootpath //projects/monolith/deploy:deploy_values)",
    },
    deps = ["@pip//pytest"],
)

semgrep_test(
    name = "chart_env_vars_test_semgrep_test",
    srcs = ["chart_env_vars_test.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "chart_env_identity_test_semgrep_test",
    srcs = ["chart_env_identity_test.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
