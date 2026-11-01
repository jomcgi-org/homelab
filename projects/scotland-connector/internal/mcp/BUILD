load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "mcp",
    srcs = [
        "documentation.go",
        "mcp.go",
        "tools.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/mcp",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/httpapi",
        "//projects/scotland-connector/internal/meter",
    ],
)

go_test(
    name = "mcp_test",
    srcs = [
        "contract_test.go",
        "mcp_test.go",
    ],
    embed = [":mcp"],
    deps = [
        "//projects/scotland-connector/internal/aurora",
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/fixture",
        "//projects/scotland-connector/internal/httpapi",
        "//projects/scotland-connector/internal/meter",
        "@com_github_prometheus_client_golang//prometheus",
    ],
)
