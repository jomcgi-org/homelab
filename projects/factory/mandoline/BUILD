load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "mandoline",
    srcs = [
        "cache.go",
        "process.go",
        "runner.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/mandoline",
    visibility = ["//projects/factory/mandoline:__subpackages__"],
)

go_test(
    name = "mandoline_test",
    srcs = ["runner_test.go"],
    data = ["//bazel/tools/mandoline:mandoline"],
    embed = [":mandoline"],
    env = {"FACTORY_TEST_MANDOLINE": "$(rootpath //bazel/tools/mandoline:mandoline)"},
)
