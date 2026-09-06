# Python files are executed through the wrappers or the explicit tests below.
# gazelle:exclude semgrep-output.py
# gazelle:exclude semgrep_wrapper_test.py
# gazelle:exclude semgrep_pro_smoke_test.py

load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files([
    "semgrep-test.sh",
    "semgrep-manifest-test.sh",
])

# keep: Gazelle's bzl extension mis-resolves @rules_shell//shell:sh_test.bzl
bzl_library(
    name = "defs",
    srcs = [
        "defs.bzl",  # keep
        "test.bzl",  # keep
    ],
    visibility = ["//visibility:public"],
    deps = [
        ":target_test",
        "@rules_shell//shell:rules_bzl",  # keep
    ],
)

# keep: Gazelle mis-resolves @rules_shell//shell:sh_test.bzl
bzl_library(
    name = "test",
    srcs = ["test.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@rules_shell//shell:rules_bzl"],  # keep
)

# keep: Gazelle mis-resolves @rules_shell//shell:sh_test.bzl
bzl_library(
    name = "target_test",
    srcs = ["target_test.bzl"],  # keep
    visibility = ["//visibility:public"],
    deps = [
        ":aspect",
        "@rules_shell//shell:rules_bzl",  # keep
    ],
)

bzl_library(
    name = "aspect",
    srcs = ["aspect.bzl"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "wrapper_support",
    srcs = [
        "semgrep-common.sh",
        "semgrep-output.py",
    ],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "wrappers",
    srcs = [
        "semgrep-manifest-test.sh",
        "semgrep-test.sh",
        ":wrapper_support",
    ],
)

py_test(
    name = "semgrep_wrapper_test",
    srcs = ["semgrep_wrapper_test.py"],
    data = [":wrappers"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "semgrep_pro_smoke_test",
    srcs = ["semgrep_pro_smoke_test.py"],
    args = ["-s"],
    data = [
        ":wrappers",
        "//bazel/semgrep/rules:kubernetes/no-privileged.yaml",
        "//bazel/semgrep/rules:python/no-shell-true.yaml",
        "//bazel/semgrep/third_party/semgrep:engine",
        "//bazel/semgrep/third_party/semgrep_pro:engine",
        "@multitool//tools/helm",
    ],
    env = {
        "SEMGREP_APP_TOKEN": "semgrep-wrapper-offline-smoke",
        "SEMGREP_SMOKE_HELM": "$(rootpath @multitool//tools/helm)",
        "SEMGREP_URL": "http://127.0.0.1:0",
    },
    deps = ["@pip//pytest"],
)
