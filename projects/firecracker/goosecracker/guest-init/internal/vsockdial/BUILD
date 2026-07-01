load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "vsockdial",
    srcs = [
        "dial_linux.go",
        "dial_other.go",
        "listen_linux.go",
        "listen_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/goosecracker/guest-init/internal/vsockdial",
    visibility = ["//projects/firecracker/goosecracker/guest-init:__subpackages__"],
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
