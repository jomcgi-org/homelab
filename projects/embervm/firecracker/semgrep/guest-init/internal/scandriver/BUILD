load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "scandriver",
    srcs = ["driver.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/firecracker/semgrep/guest-init/internal/scandriver",
    visibility = ["//projects/embervm/firecracker/semgrep/guest-init:__subpackages__"],
    deps = [
        "//projects/embervm/firecracker/semgrep/guest-init/internal/cliout",
        "//projects/embervm/firecracker/substrate/vsockproto",
    ],
)

go_test(
    name = "scandriver_test",
    srcs = ["driver_test.go"],
    embed = [":scandriver"],
    deps = ["//projects/embervm/firecracker/substrate/vsockproto"],
)
