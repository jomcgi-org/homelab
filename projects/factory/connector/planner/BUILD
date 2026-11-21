load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "planner",
    srcs = [
        "proposals.go",
        "schedule.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/planner",
    visibility = ["//visibility:public"],
)

go_test(
    name = "planner_test",
    srcs = ["proposals_test.go"],
    embed = [":planner"],
)
