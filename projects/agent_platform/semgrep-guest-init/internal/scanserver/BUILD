load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "scanserver",
    srcs = [
        "listen_linux.go",
        "listen_other.go",
        "scanserver.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/semgrep-guest-init/internal/scanserver",
    visibility = ["//projects/agent_platform/semgrep-guest-init:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/vsockproto",
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

go_test(
    name = "scanserver_test",
    srcs = ["scanserver_test.go"],
    embed = [":scanserver"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)
