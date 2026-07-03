load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/monolith/whatsapp/cmd",
    visibility = ["//visibility:private"],
    deps = ["//projects/monolith/whatsapp"],
)

go_binary(
    name = "whatsapp",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
