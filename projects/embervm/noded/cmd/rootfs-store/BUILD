load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

# Target names follow gazelle's convention for this directory (rootfs-store_lib,
# rootfs-store, rootfs-store_test) so CI's //:gazelle run is a no-op here; a
# hand-picked name made gazelle add a second test embedding a label that did
# not exist and failed analysis in the merge queue.
go_library(
    name = "rootfs-store_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/cmd/rootfs-store",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/embervm/noded/sparse",
        "//projects/embervm/noded/store",
    ],
)

go_binary(
    name = "rootfs-store",
    embed = [":rootfs-store_lib"],
    visibility = ["//projects/firecracker/substrate/rootfs-builder:__pkg__"],
)

go_test(
    name = "rootfs-store_test",
    srcs = [
        "main_sparse_linux_test.go",
        "main_test.go",
    ],
    embed = [":rootfs-store_lib"],
)
