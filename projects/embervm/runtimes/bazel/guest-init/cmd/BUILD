load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

# ember-bazel-init: PID 1 of the bazel-query demo microVM (ADR embervm/010).
# Mounts /proc + a large tmpfs /tmp, runs the warming `bazel cquery //absl/...`,
# flips /shim/ready only after the warming client exits (so BuildBase snapshots a
# WARM bazel server), then serves one POST /query per restored clone over vsock.
# Cross-compiled per arch and layered into the runtime image at
# /usr/local/bin/ember-bazel-init (see ../../BUILD). Mirrors the postgres/python
# guest-init layout.
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
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/bazel/guest-init/cmd",
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

go_test(
    name = "cmd_test",
    srcs = [
        "main_test.go",
        "net_linux_test.go",
    ],
    embed = [":cmd_lib"],
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
