load("//bazel/helm:defs.bzl", "argocd_app")
load("//bazel/tools/pytest:defs.bzl", "py_test")

argocd_app(
    name = "otel-collector",
    chart = "projects/platform/otel-collector",
    chart_files = "//projects/platform/otel-collector:chart",
    namespace = "otel-collector",
    release_name = "otel-collector",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/otel-collector:values.yaml",
        "//projects/platform/otel-collector:values-factory.yaml",
    ],
)

py_test(
    name = "ingress_test",
    srcs = ["ingress_test.py"],
    data = ["resources/network-policy.yaml"],
    deps = ["@pip//pyyaml"],
)
