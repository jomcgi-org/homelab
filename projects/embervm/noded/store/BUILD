load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "store",
    srcs = [
        "aead.go",
        "sigv4.go",
        "store.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/store",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = [
        "//projects/embervm/noded/sparse",
        "@com_github_klauspost_compress//zstd",
    ],
)

go_test(
    name = "store_test",
    srcs = [
        "aead_test.go",
        "restore_sparse_linux_test.go",
        "sigv4_test.go",
        "store_test.go",
    ],
    embed = [":store"],
)
