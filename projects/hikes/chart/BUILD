load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    images = {"image": "//projects/hikes:image.info"},
    # Strict digest validation needs the build-time image values below.
    lint = False,  # keep
    publish = True,
    visibility = ["//visibility:public"],
)

filegroup(
    name = "image_values",
    srcs = [":chart.images_values"],
    visibility = ["//projects/hikes/deploy:__pkg__"],
)

py_test(
    name = "jobs_test",
    srcs = ["jobs_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**",
        "charts/**",
    ]),
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = ["@pip//pyyaml"],
)
