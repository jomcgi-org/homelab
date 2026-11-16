load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "buildbuddy",
    srcs = [
        "base.go",
        "buildbuddy.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/buildbuddy",
    visibility = ["//visibility:public"],
    deps = ["@com_github_bazelbuild_buildtools//build"],
)

go_test(
    name = "buildbuddy_test",
    srcs = [
        "base_test.go",
        "buildbuddy_test.go",
    ],
    embed = [":buildbuddy"],
)
