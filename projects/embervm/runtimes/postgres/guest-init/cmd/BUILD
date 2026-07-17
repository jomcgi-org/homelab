load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

# ember-postgres-init: PID 1 of the R4 scratch-postgres stateful microVM (ADR
# embervm/001, D-R4.PR-11.1). Off ONE rootfs it serves two boot classes: a base
# build (no volume boot-arg -> answer the vsock /shim/ready contract so noded
# snapshots the warm base) and a stateful cold boot (ember.volume_dev/ember.env.*
# present -> mount the volume, decode POSTGRES_PASSWORD from mmds_env, initdb-if-
# empty, launch postgres on TCP 5432). Cross-compiled per arch and layered into
# the runtime-postgres image at /usr/local/bin/ember-postgres-init (see ../../
# BUILD), which the noded harnessInit boots via init=<path>. Reuses the frozen
# substrate shim + vsockproto for the vsock ready server.
go_library(
    name = "cmd_lib",
    srcs = [
        "cmdline.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "postgres_linux.go",
        "postgres_other.go",
        "vsock_linux.go",
        "vsock_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/postgres/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/substrate/shim",
        "//projects/firecracker/substrate/vsockproto",
    ] + select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix:go_default_library",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix:go_default_library",
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
        "main_test.go",
        "postgres_linux_test.go",
    ],
    embed = [":cmd_lib"],
)
