load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "mcp",
    srcs = [
        "mcp.go",
        "tools.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/mcp",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/meter",
    ],
)

go_test(
    name = "mcp_test",
    srcs = [
        "mcp_test.go",
        "observer_test.go",
    ],
    data = [
        "//projects/scotland-connector/chart:observer_test_inputs",
        "@envoy_mcp_test_bin//:binary",
        "@multitool//tools/helm",
    ],
    env = {
        "ENVOY_MCP_BIN": "$(rootpath @envoy_mcp_test_bin//:binary)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "OBSERVER_CHART": "projects/scotland-connector/chart",
    },
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    embed = [":mcp"],
    deps = [
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/fixture",
        "//projects/scotland-connector/internal/meter",
        "@com_github_prometheus_client_golang//prometheus",
    ],
)
