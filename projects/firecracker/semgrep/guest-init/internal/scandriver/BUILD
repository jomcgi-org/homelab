load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "scandriver",
    srcs = ["driver.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/scandriver",
    visibility = ["//projects/firecracker/semgrep/guest-init:__subpackages__"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)
