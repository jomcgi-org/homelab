load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

# ember-iggy-init: PID 1 of the R4 iggy stateful microVM (ADR embervm/039). Off
# ONE rootfs it serves two boot classes: a base build (no volume boot-arg ->
# answer the vsock /shim/ready contract so noded snapshots the warm base) and a
# stateful cold boot (ember.volume_dev/ember.env.* present -> mount the volume,
# decode IGGY_ROOT_PASSWORD from mmds_env, launch iggy-server on TCP 8090).
# Cross-compiled per arch and layered into the runtime-iggy image at
# /usr/local/bin/ember-iggy-init (see ../../BUILD), which the noded harnessInit
# boots via init=<path>. Reuses the frozen substrate shim + vsockproto for the
# vsock ready server, exactly as the postgres runtime's init does.
go_library(
    name = "cmd_lib",
    srcs = [
        "cmdline.go",
        "iggy.go",
        "iggy_linux.go",
        "iggy_other.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "vsock_linux.go",
        "vsock_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/iggy/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/substrate/shim",
        "//projects/firecracker/substrate/vsockproto",
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
        "iggy_test.go",
        "main_test.go",
    ],
    embed = [":cmd_lib"],
)
