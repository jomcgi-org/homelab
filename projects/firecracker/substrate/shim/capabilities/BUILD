load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "capabilities",
    srcs = [
        "git.go",
        "objectstore.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/shim/capabilities",
    visibility = ["//visibility:public"],
)

go_test(
    name = "capabilities_test",
    srcs = [
        "git_test.go",
        "objectstore_test.go",
    ],
    embed = [":capabilities"],
)
