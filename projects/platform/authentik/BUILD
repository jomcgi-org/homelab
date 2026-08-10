load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "authentik",
    chart = "projects/platform/authentik",
    chart_files = ":chart",
    namespace = "authentik",
    release_name = "authentik",
    semgrep_exclude_rules = [
        "require-resource-limits",
        "require-readiness-probe",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = ["values.yaml"],
)

# Hand-written test target because this directory is gazelle-excluded for Python.
# The `data` glob materializes the blueprints/*.yaml files as runfiles so the
# test can read them during execution. See blueprint_tags_test.py for why this
# test exists: it guards against malformed !Env tags breaking blueprints discovery.
py_test(
    name = "blueprint_tags_test",
    srcs = ["blueprint_tags_test.py"],
    data = glob(["blueprints/*.yaml"]),
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
