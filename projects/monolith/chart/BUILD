load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/helm:defs.bzl", "helm_chart")

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

load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "chart_env_vars_test",
    srcs = ["chart_env_vars_test.py"],
    data = [
        "values.yaml",
        "Chart.yaml",
        "Chart.lock",
        "//projects/monolith:module_configs",
        "//projects/monolith/deploy:deploy_values",
        "@multitool//tools/helm",
    ] + glob(["templates/**/*", "charts/**/*", "migrations/**/*"]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "DEPLOY_VALUES": "$(rootpath //projects/monolith/deploy:deploy_values)",
    },
)
