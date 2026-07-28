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

# Exposed so the ADR 023 6b cleartext-lane guard one package up can read
# ANTHROPIC_BASE_URL out of setDefaultEnv. That is the only place the guest
# actually receives it: apko's environment block is OCI image config, which a raw
# Firecracker boot never reads.
filegroup(
    name = "guest_init_source",
    srcs = ["main.go"],
    visibility = ["//projects/embervm/runtimes/claude:__pkg__"],
)
