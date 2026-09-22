load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "driver",
    srcs = [
        "cgroup_linux.go",
        "cgroup_other.go",
        "driver.go",
        "gueststats.go",
        "jail.go",
        "jail_mount_linux.go",
        "jail_mount_other.go",
        "launcher.go",
        "logtag.go",
        "mountns_linux.go",
        "mountns_other.go",
        "provisioner.go",
        "reflink_linux.go",
        "reflink_other.go",
        "serial.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/fcvm/driver",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = [
        "//projects/embervm/noded/fcvm/fcclient",
        "//projects/embervm/noded/snapshotmeta",
        "//projects/embervm/noded/sparse",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/noded/vsockproto",
        "@io_opentelemetry_go_otel//:otel",
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

go_test(
    name = "driver_test",
    srcs = [
        "cgroup_linux_test.go",
        "driver_test.go",
        "group_test.go",
        "gueststats_test.go",
        "launcher_linux_test.go",
        "launcher_test.go",
        "logtag_test.go",
        "provisioner_test.go",
        "reflink_linux_test.go",
        "serial_test.go",
    ],
    embed = [":driver"],
    deps = [
        "//projects/embervm/noded/fcvm/fcclient",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/noded/vsockproto",
        "@io_opentelemetry_go_otel//:otel",
        "@io_opentelemetry_go_otel_sdk//trace",
        "@io_opentelemetry_go_otel_sdk//trace/tracetest",
        "@io_opentelemetry_go_otel_trace//:trace",
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
