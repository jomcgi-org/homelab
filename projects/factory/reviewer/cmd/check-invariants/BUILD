load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "check-invariants_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/reviewer/cmd/check-invariants",
    visibility = ["//visibility:private"],
    deps = ["//projects/factory/reviewer/review"],
)

go_binary(
    name = "check-invariants",
    embed = [":check-invariants_lib"],
    visibility = ["//visibility:public"],
)
