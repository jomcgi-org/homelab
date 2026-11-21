load("//bazel/tools/pytest:defs.bzl", "py_test")

# The preflight runs directly from a checkout. Exercise that same script in runfiles.
# gazelle:exclude dns_preflight.py

py_test(
    name = "dns_preflight_test",
    srcs = ["dns_preflight_test.py"],
    data = ["dns_preflight.py"],
    deps = ["@pip//pyyaml"],
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
