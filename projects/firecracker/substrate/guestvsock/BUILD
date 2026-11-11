load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "guestvsock",
    srcs = ["listener_linux.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/guestvsock",
    visibility = ["//visibility:public"],
    deps = select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
)

go_test(
    name = "guestvsock_test",
    srcs = ["listener_linux_test.go"],
    embed = [":guestvsock"],
    deps = select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
)
