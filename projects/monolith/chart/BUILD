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
    },
    lint = False,
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
