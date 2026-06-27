load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "vsockdial",
    srcs = [
        "dial_linux.go",
        "dial_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/vsockdial",
    visibility = ["//projects/agent_platform/fc-agent-init:__subpackages__"],
    deps = select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
)
