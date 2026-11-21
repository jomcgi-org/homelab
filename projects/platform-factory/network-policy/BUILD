load("//bazel/tools/pytest:defs.bzl", "py_test")

# The preflight runs directly from a checkout. Exercise that same script in runfiles.
# gazelle:exclude dns_preflight.py

py_test(
    name = "dns_preflight_test",
    srcs = ["dns_preflight_test.py"],
    data = [
        "dns_preflight.py",
        "resources/baseline.yaml",
    ],
    deps = ["@pip//pyyaml"],
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)

exports_files(
    ["resources/argocd.yaml"],
    visibility = [
        "//projects/platform-factory/external-secrets/chart:__pkg__",
        "//projects/platform-factory/network-policy/tests:__pkg__",
    ],
)

exports_files(
    [
        "resources/kube-system.yaml",
        "resources/onepassword.yaml",
    ],
    visibility = ["//projects/platform-factory/network-policy/tests:__pkg__"],
)
