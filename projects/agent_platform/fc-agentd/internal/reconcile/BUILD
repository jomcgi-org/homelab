load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "reconcile",
    srcs = ["reconcile.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/reconcile",
    visibility = ["//projects/agent_platform/fc-agentd:__subpackages__"],
    deps = [
        "//projects/agent_platform/fc-agentd/internal/control",
        "//projects/agent_platform/fc-agentd/internal/store",
        "//projects/agent_platform/substrate",
        "//projects/agent_platform/vsockproto",
        "@io_opentelemetry_go_otel//:otel",
    ],
)

go_test(
    name = "reconcile_test",
    srcs = ["reconcile_test.go"],
    embed = [":reconcile"],
    deps = [
        "//projects/agent_platform/fc-agentd/internal/store",
        "//projects/agent_platform/substrate",
    ],
)
