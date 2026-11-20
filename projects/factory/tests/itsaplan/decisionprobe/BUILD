load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_binary(
    name = "decisionprobe",
    testonly = True,
    embed = [":decisionprobe_lib"],
    visibility = ["//projects/factory/tests/itsaplan:__pkg__"],
)

go_library(
    name = "decisionprobe_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/tests/itsaplan/decisionprobe",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/store",
    ],
)
