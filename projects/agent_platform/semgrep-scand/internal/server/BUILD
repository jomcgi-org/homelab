load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "server",
    srcs = ["server.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/internal/server",
    visibility = ["//projects/agent_platform/semgrep-scand:__subpackages__"],
    deps = ["//projects/agent_platform/vsockproto"],
)

go_test(
    name = "server_test",
    srcs = ["server_test.go"],
    embed = [":server"],
    deps = ["//projects/agent_platform/vsockproto"],
)
