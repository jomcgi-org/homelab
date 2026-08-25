load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "signoz",
    chart = "projects/platform/signoz",
    chart_files = ":chart",
    namespace = "signoz",
    release_name = "signoz",
    # Many subcomponents (clickhouse, alertmanager, otel collectors); fixing all via values is fragile; resource limits not configurable for all subcomponent containers; async/wget containers also lack readiness probes
    semgrep_exclude_rules = [
        "require-tmp-emptydir",
        "require-resource-limits",
        "require-readiness-probe",
        "yaml.kubernetes.security.allow-privilege-escalation.allow-privilege-escalation",
        "yaml.kubernetes.security.writable-filesystem-container.writable-filesystem-container",
        "yaml.kubernetes.security.run-as-non-root.run-as-non-root",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-prod.yaml",
    ],
)

# Hand-written test target (this directory has no gazelle-managed Python): the
# alert ConfigMaps, the httpcheck probe values, and the kustomization must stay
# consistent or #4867's blind spot (public /health failing unalerted) returns.
# The data glob materializes the YAML files as runfiles so the test can read
# them during execution. See signoz_alerts_test.py for what each guard pins.
py_test(
    name = "signoz_alerts_test",
    srcs = ["signoz_alerts_test.py"],
    data = glob(["*.yaml"]),
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

semgrep_test(
    name = "signoz_alerts_test_semgrep_test",
    srcs = ["signoz_alerts_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

# Hand-written test target (no gazelle here): guards the #4659 chain, where the
# alert is only as good as the metric underneath it. Besides this package's
# YAML it reads the Cilium values and bootstrap HelmChart (the drop metric's
# context labels live there), hence the cross-package data deps.
py_test(
    name = "hubble_monolith_policy_drops_test",
    srcs = ["hubble_monolith_policy_drops_test.py"],
    data = glob(["*.yaml"]) + [
        "//projects/platform/cilium:bootstrap/cilium-helmchart.yaml",
        "//projects/platform/cilium:values.yaml",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

semgrep_test(
    name = "hubble_monolith_policy_drops_test_semgrep_test",
    srcs = ["hubble_monolith_policy_drops_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
