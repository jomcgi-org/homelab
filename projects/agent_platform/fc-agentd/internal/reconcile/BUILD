load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "reconcile",
    srcs = ["reconcile.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/reconcile",
    visibility = ["//projects/agent_platform/fc-agentd:__subpackages__"],
    deps = [
        "//projects/agent_platform/fc-agentd/internal/control",
        "//projects/agent_platform/fc-agentd/internal/store",
        "//projects/firecracker/substrate/substrate",
        "//projects/firecracker/substrate/vsockproto",
        "@io_opentelemetry_go_otel//:otel",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel//codes",
    ],
)

go_test(
    name = "reconcile_test",
    srcs = ["reconcile_test.go"],
    embed = [":reconcile"],
    deps = [
        "//projects/agent_platform/fc-agentd/internal/store",
        "//projects/firecracker/substrate/substrate",
    ],
)
