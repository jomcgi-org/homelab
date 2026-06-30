load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = [
        "dial_linux.go",
        "dial_other.go",
        "loopback_linux.go",
        "loopback_other.go",
        "main.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/semgrep-guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/agent_platform/semgrep-guest-init/internal/lspdriver",
        "//projects/agent_platform/semgrep-guest-init/internal/scanserver",
        "//projects/agent_platform/vsockproto",
    ] + select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
