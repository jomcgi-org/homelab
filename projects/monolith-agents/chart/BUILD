load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart", "helm_template_test")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    images = {
        "agents.image": "//projects/monolith:main_agents_image.info",
    },
    lint = False,
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)

helm_template_test(
    name = "chart_smoke_test",
    chart = "projects/monolith-agents/chart",
    chart_files = ":chart",
    namespace = "monolith-agents",
    release_name = "monolith-agents",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/monolith-agents/deploy:deploy_values"],
)

py_test(
    name = "chart_render_test",
    srcs = ["test_chart_render.py"],
    data = [
        ":chart",
        "//projects/monolith-agents/deploy:deploy_values",
        "@multitool//tools/helm",
    ],
    env = {
        "DEPLOY_VALUES": "$(rootpath //projects/monolith-agents/deploy:deploy_values)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

argocd_app(
    name = "monolith-agents",
    chart = "projects/monolith-agents/chart",
    chart_files = ":chart",
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "monolith-agents",
    release_name = "monolith-agents",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/monolith-agents/deploy:deploy_values"],
)

semgrep_test(
    name = "test_chart_render_semgrep_test",
    srcs = ["test_chart_render.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

py_test(
    name = "test_chart_render",
    srcs = ["test_chart_render.py"],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
