load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "driver",
    srcs = [
        "driver.go",
        "gueststats.go",
        "launcher.go",
        "mountns_linux.go",
        "mountns_other.go",
        "provisioner.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/node/fcvm/driver",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/firecracker/substrate/node/fcvm/fcclient",
        "//projects/firecracker/substrate/substrate",
        "@io_opentelemetry_go_otel//:go_default_library",
    ],
)

go_test(
    name = "driver_test",
    srcs = [
        "driver_test.go",
        "gueststats_test.go",
        "provisioner_test.go",
    ],
    embed = [":driver"],
    deps = ["//projects/firecracker/substrate/substrate"],
)
