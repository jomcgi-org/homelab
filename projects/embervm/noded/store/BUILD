load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "store",
    srcs = ["store.go"],
    deps = ["@com_github_klauspost_compress//zstd"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/store",
    visibility = ["//projects/embervm/noded:__subpackages__"],
)

go_test(
    name = "store_test",
    srcs = ["store_test.go"],
    embed = [":store"],
)
