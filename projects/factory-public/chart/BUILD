load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    publish = True,
    visibility = ["//visibility:public"],
)

py_test(
    name = "public_test",
    srcs = ["public_test.py"],
    data = [
        ":chart",
        "//projects/factory-public/deploy:application.yaml",
        "@multitool//tools/helm",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "PUBLIC_APPLICATION": "$(rootpath //projects/factory-public/deploy:application.yaml)",
    },
    deps = ["@pip//pyyaml"],
)
