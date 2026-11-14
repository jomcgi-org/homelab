load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "status",
    srcs = ["status.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/status",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/connector/config",
        "//projects/factory/connector/github",
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/store",
    ],
)

go_test(
    name = "status_test",
    srcs = ["status_test.go"],
    embed = [":status"],
    deps = [
        "//projects/factory/connector/config",
        "//projects/factory/connector/github",
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/store",
    ],
)
