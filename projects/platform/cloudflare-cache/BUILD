load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "cloudflare_cache_lib",
    srcs = ["cloudflare_cache.py"],
    data = ["policy.json"],
    imports = ["."],
)

py_test(
    name = "cloudflare_cache_test",
    srcs = ["cloudflare_cache_test.py"],
    data = ["policy.json"],
    deps = [
        ":cloudflare_cache_lib",
        "@pip//pytest",
    ],
)

semgrep_test(
    name = "cloudflare_cache_test_semgrep_test",
    srcs = [
        "cloudflare_cache.py",
        "cloudflare_cache_test.py",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
