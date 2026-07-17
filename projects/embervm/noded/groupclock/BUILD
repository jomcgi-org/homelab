load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "groupclock",
    srcs = [
        "clock.go",
        "vsock.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/groupclock",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = ["//projects/embervm/noded/vsockproto"],
)

go_test(
    name = "groupclock_test",
    srcs = ["clock_test.go"],
    embed = [":groupclock"],
)
