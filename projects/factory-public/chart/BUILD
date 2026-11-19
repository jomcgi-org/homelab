# templates/buckets.yaml passes this script to the health CronJob.
# gazelle:exclude files/health.py

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
        "//projects/shared/helm/factory-library:fake_rclone.py",
        "@multitool//tools/helm",
    ],
    env = {
        "FAKE_RCLONE": "$(rootpath //projects/shared/helm/factory-library:fake_rclone.py)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "PUBLIC_APPLICATION": "$(rootpath //projects/factory-public/deploy:application.yaml)",
    },
    deps = ["@pip//pyyaml"],
)

py_test(
    name = "health_test",
    srcs = ["health_test.py"],
    data = ["files/health.py"],
)
