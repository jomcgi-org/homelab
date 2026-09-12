load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "cloudflare_cache_test",
    srcs = [
        "cloudflare_cache.py",
        "cloudflare_cache_test.py",
    ],
    data = ["policy.json"],
    deps = ["@pip//pytest"],
)

semgrep_test(
    name = "cloudflare_cache_test_semgrep_test",
    srcs = [
        "cloudflare_cache.py",
        "cloudflare_cache_test.py",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
