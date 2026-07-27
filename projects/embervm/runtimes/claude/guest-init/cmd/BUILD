load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = [
        "exec_linux.go",
        "exec_other.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "net_linux.go",
        "net_other.go",
        "volume_linux.go",
        "volume_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/claude/guest-init/cmd",
    visibility = ["//visibility:private"],
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

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = ["main_test.go"],
    embed = [":cmd_lib"],
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
