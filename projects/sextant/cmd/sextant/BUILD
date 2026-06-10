load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "sextant_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/sextant/cmd/sextant",
    visibility = ["//visibility:private"],
    deps = ["//projects/sextant/cmd/sextant/cmd"],
)

go_binary(
    name = "sextant",
    embed = [":sextant_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "main_test",
    srcs = ["main_test.go"],
    embed = [":sextant_lib"],
)

go_test(
    name = "sextant_test",
    srcs = ["main_test.go"],
    embed = [":sextant_lib"],
)
