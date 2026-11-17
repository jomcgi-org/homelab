load("//bazel/helm:defs.bzl", "helm_chart", "helm_lint_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    images = {"image": "//projects/jomcgi-site:image.info"},
    # Strict digest validation needs the build-time image values below.
    lint = False,  # keep
    publish = True,
    visibility = ["//visibility:public"],
)

helm_lint_test(
    name = "lint_test",
    extra_values = [":chart.images_values"],
    tags = [
        "chart",
        "lint",
    ],
)

filegroup(
    name = "image_values",
    srcs = [":chart.images_values"],
    visibility = ["//projects/jomcgi-site/deploy:__pkg__"],
)

py_test(
    name = "site_chart_test",
    srcs = ["site_chart_test.py"],
    data = [
        ":chart",
        "@multitool//tools/helm",
    ],
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
