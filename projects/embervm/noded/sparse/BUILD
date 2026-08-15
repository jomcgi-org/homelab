load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "sparse",
    srcs = [
        "sparse.go",
        "sparse_linux.go",
        "sparse_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/sparse",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = select({
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
    name = "sparse_test",
    srcs = ["sparse_linux_test.go"],
    embed = [":sparse"],
    deps = select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
)
