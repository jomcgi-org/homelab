load("@rules_go//go:def.bzl", "go_binary", "go_library")

# ember-shotter-init is PID 1 of the shotter task guest. It is cross-compiled
# for the only supported guest architecture and layered into the apko image by
# ../../BUILD.
go_library(
    name = "cmd_lib",
    srcs = [
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "net_linux.go",
        "net_other.go",
        "vsock_linux.go",
        "vsock_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/shotter/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/substrate/vsockproto",
        "@org_golang_x_sys//unix",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
