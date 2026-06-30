load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "scanner",
    srcs = [
        "scanner.go",
        "transport.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/internal/scanner",
    visibility = ["//projects/agent_platform/semgrep-scand:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/substrate",
        "//projects/firecracker/substrate/vsockproto",
        "@org_golang_x_sync//semaphore",
    ],
)

go_test(
    name = "scanner_test",
    srcs = ["scanner_test.go"],
    embed = [":scanner"],
    deps = [
        "//projects/firecracker/substrate/substrate",
        "//projects/firecracker/substrate/vsockproto",
    ],
)
