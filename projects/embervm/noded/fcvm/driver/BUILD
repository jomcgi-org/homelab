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
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/fcvm/driver",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = [
        "//projects/embervm/noded/fcvm/fcclient",
        "//projects/embervm/noded/substrate",
        "@io_opentelemetry_go_otel//:otel",
    ],
)

go_test(
    name = "driver_test",
    srcs = [
        "driver_test.go",
        "group_test.go",
        "gueststats_test.go",
        "provisioner_test.go",
    ],
    embed = [":driver"],
    deps = ["//projects/embervm/noded/substrate"],
)
