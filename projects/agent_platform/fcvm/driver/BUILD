load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "driver",
    srcs = [
        "driver.go",
        "launcher.go",
        "mountns_linux.go",
        "mountns_other.go",
        "provisioner.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fcvm/driver",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/agent_platform/fcvm/fcclient",
        "//projects/agent_platform/substrate",
        "@io_opentelemetry_go_otel//:otel",
    ],
)

go_test(
    name = "driver_test",
    srcs = [
        "driver_test.go",
        "provisioner_test.go",
    ],
    embed = [":driver"],
    deps = ["//projects/agent_platform/substrate"],
)
