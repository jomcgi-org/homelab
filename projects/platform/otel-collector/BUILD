load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "otel-collector",
    chart = "projects/platform/otel-collector",
    chart_files = ":chart",
    namespace = "otel-collector",
    release_name = "otel-collector",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-prod.yaml",
    ],
)

# Hand-written test target (this directory has no gazelle-managed Python).
# argocd_app only proves the chart renders; nothing there parses the embedded
# collector config. This pins the invariant the chart exists to enforce: with
# an empty allowlist there is no otlp receiver, no traces pipeline, and no OTLP
# port, and the metrics pipeline never accepts arbitrary OTLP. The data glob
# materializes the chart as runfiles so helm can render it during the test.
py_test(
    name = "collector_optin_test",
    srcs = ["collector_optin_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "values-prod.yaml",
        "@multitool//tools/helm",
    ] + glob(["templates/**/*"]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

semgrep_test(
    name = "collector_optin_test_semgrep_test",
    srcs = ["collector_optin_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
