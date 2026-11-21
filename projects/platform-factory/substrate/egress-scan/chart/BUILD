load("//bazel/helm:defs.bzl", "helm_chart", "helm_lint_test")

helm_chart(
    name = "chart",
    images = {"image": "//projects/platform-factory/substrate/egress-scan:image.info"},
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
    visibility = ["//projects/platform-factory/substrate/resources:__pkg__"],
)
