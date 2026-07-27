load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "guestboot",
    srcs = [
        "guestboot.go",
        "loopback_linux.go",
        "loopback_other.go",
        "mount_linux.go",
        "mount_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/guestboot",
    visibility = ["//projects/firecracker/semgrep/guest-init:__subpackages__"],
    deps = select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix:go_default_library",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix:go_default_library",
        ],
        "//conditions:default": [],
    }),
)
