# gazelle:exclude **/*.bzl
load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("@multitool//:tools.bzl", MULTITOOL_TOOLS = "TOOLS")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

exports_files([
    "chart-version.sh",
    "check-missed-bump.sh",
    "helm-assert-annotations.sh",
    "helm-template-test.sh",
    "push.sh.tpl",
    "render-manifests.sh",
])

sh_test(
    name = "check_missed_bump_test",
    srcs = ["check-missed-bump_test.sh"],
    data = [":check-missed-bump.sh"],
)

sh_test(
    name = "chart_version_test",
    srcs = ["chart-version_test.sh"],
    data = [":chart-version.sh"],
)

sh_binary(
    name = "validate_manifests",
    srcs = ["ci-validate-manifests.sh"],
    data = [MULTITOOL_TOOLS["helm"]],
    env = {
        "HELM": "$(rootpath {})".format(MULTITOOL_TOOLS["helm"]),
    },
)

sh_binary(
    name = "render_all",
    srcs = ["render-all-manifests.sh"],
    data = [MULTITOOL_TOOLS["helm"]],
    env = {
        "HELM": "$(rootpath {})".format(MULTITOOL_TOOLS["helm"]),
    },
    visibility = ["//bazel/tools/format:__pkg__"],
)

# keep: Gazelle's bzl extension mis-resolves @rules_shell//shell:sh_test.bzl
bzl_library(
    name = "defs",
    srcs = [
        "app.bzl",  # keep
        "chart.bzl",  # keep
        "defs.bzl",  # keep
        "images.bzl",  # keep
        "push.bzl",  # keep
        "render.bzl",  # keep
        "test.bzl",  # keep
    ],
    visibility = ["//visibility:public"],
    deps = [
        "//bazel/semgrep/defs",  # keep
        "//bazel/tools/oci:providers",  # keep
        "@bazel_skylib//lib:paths",  # keep
        "@rules_shell//shell:rules_bzl",  # keep
    ],
)
