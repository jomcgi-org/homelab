load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Guard (ADR 005): the public HTTPRoute must never route chat or the internal
# API. Reads the template and fails CI if a future edit exposes either.
py_test(
    name = "httproute_public_test",
    srcs = ["httproute_public_test.py"],
    data = ["templates/httproute-public.yaml"],
    imports = ["."],
)

helm_chart(
    name = "chart",
    images = {
        "web.image": "//projects/monolith:image_public.info",
        "frontend.image": "//projects/monolith/frontend:image_public.info",
    },
    lint = False,
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)

semgrep_test(
    name = "httproute_public_test_semgrep_test",
    srcs = ["httproute_public_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
