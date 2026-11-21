load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "receiver_lib",
    srcs = [
        "http.go",
        "main.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/push-guard/receiver",
    visibility = ["//visibility:private"],
)

go_binary(
    name = "receiver",
    embed = [":receiver_lib"],
    pure = "on",
    visibility = ["//projects/factory/push-guard:__subpackages__"],
)

go_test(
    name = "receiver_test",
    srcs = ["http_test.go"],
    data = ["//projects/factory/push-guard:pre-receive"],
    embed = [":receiver_lib"],
    env = {"PUSH_GUARD_TEST_HOOK": "$(rootpath //projects/factory/push-guard:pre-receive)"},
)
