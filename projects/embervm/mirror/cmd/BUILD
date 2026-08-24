load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/mirror/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/embervm/mirror/refresh",
        "//projects/embervm/mirror/server",
    ],
)

go_binary(
    name = "git-mirror",
    embed = [":cmd_lib"],
    visibility = ["//projects/embervm/mirror:__subpackages__"],
)
