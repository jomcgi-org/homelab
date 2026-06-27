load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "store",
    srcs = ["store.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/store",
    visibility = ["//projects/agent_platform/fc-agentd:__subpackages__"],
    deps = [
        "//projects/agent_platform/substrate",
        "@com_github_jackc_pgx_v5//pgxpool",
    ],
)
