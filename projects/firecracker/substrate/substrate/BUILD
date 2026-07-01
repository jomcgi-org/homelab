load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "substrate",
    srcs = [
        "executor.go",
        "fake.go",
        "substrate.go",
        "workload.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/substrate",
    visibility = ["//visibility:public"],
)

go_test(
    name = "substrate_test",
    srcs = ["fake_test.go"],
    embed = [":substrate"],
)
