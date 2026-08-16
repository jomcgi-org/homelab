load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

# ember-shotter-init is PID 1 of the shotter task guest. It is cross-compiled
# for the only supported guest architecture and layered into the apko image by
# ../../BUILD.
go_library(
    name = "cmd_lib",
    srcs = [
        "cdp.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "net_linux.go",
        "net_other.go",
        "proxy.go",
        "screenshot.go",
        "vsock_linux.go",
        "vsock_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/shotter/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/substrate/vsockproto",
        "@io_nhooyr_websocket//:websocket",
    ] + select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = [
        "proxy_test.go",
        "screenshot_test.go",
    ],
    embed = [":cmd_lib"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)
