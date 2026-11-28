load("@rules_go//go:def.bzl", "go_library", "go_test")
load("//projects/factory/discord/libs:libs.bzl", "LIBS")

exports_files(
    [
        "examples/comparison.html",
        "examples/explainer.html",
        "examples/report.html",
        "template.html",
    ],
    visibility = ["//projects/factory/discord/libs:__pkg__"],
)

go_library(
    name = "artifacts",
    srcs = [
        "artifacts.go",
        "lint.go",
        "mcp.go",
        "s3.go",
        "share.go",
    ],
    embedsrcs = [
        "brief.md",
        "examples/comparison.html",
        "examples/explainer.html",
        "examples/report.html",
        "template.html",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/artifacts",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "@org_golang_x_net//html",
    ],
)

go_test(
    name = "artifacts_test",
    srcs = [
        "artifacts_test.go",
        "lint_test.go",
        "share_test.go",
        "sigv4_test.go",
    ],
    data = ["//projects/factory/discord/libs:selftest/architecture.html"],
    embed = [":artifacts"],
    env = {
        # The files the service serves at /_lib/, to find the newest house-N.
        "LIB_NAMES": " ".join(LIBS.keys()),
        "SELFTEST_PAGE": "$(rootpath //projects/factory/discord/libs:selftest/architecture.html)",
    },
    deps = ["//projects/factory/broker/actorjwt"],
)
