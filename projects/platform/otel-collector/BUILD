load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

# The prod opt-in list, exposed to the monolith's chart tests. The allowlist and
# a sender's endpoint live in two charts that never see each other, and a
# mismatch drops every span with no error, so the monolith asserts its own
# service name is on this list. Narrow visibility on purpose: this is a
# named-consumer edge, not a general export.
filegroup(
    name = "prod_values",
    srcs = ["values-prod.yaml"],
    visibility = ["//projects/monolith/chart:__pkg__"],
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

argocd_app(
    name = "otel-collector-gke",
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
        "values-gke.yaml",
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
        "Chart.lock",
        "Chart.yaml",
        "values.yaml",
        "values-prod.yaml",
        "@multitool//tools/helm",
        # Chart.lock plus the vendored charts/ tree are load-bearing for
        # `helm template`: without them the render aborts on the cf-ingress
        # subchart dependency that the public health route uses.
    ] + glob([
        "templates/**/*",
        "charts/**/*",
    ]),
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
