load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "handler",
    srcs = [
        "handler.go",
        "lang.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/sandbox/guest-init/internal/handler",
    visibility = ["//projects/firecracker/sandbox/guest-init:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/shim",
    ],
)

go_test(
    name = "handler_test",
    srcs = [
        "handler_test.go",
        "lang_test.go",
    ],
    embed = [":handler"],
    deps = [
        "//projects/firecracker/substrate/shim",
    ],
)
