load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

# ember-runtime-guest-init: PID 1 of the R1 zip-lane runtime-python microVM
# (ADR embervm/002). Mounts a tmpfs over /tmp then execs the python bootstrap
# shim. Cross-compiled per arch and layered into the runtime-python image at
# /usr/local/bin/ember-runtime-guest-init (see ../../BUILD), which the noded
# harnessInit boots via init=<path>. Mirrors the sandbox guest-init layout.
go_library(
    name = "cmd_lib",
    srcs = [
        "exec_linux.go",
        "exec_other.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "volume_linux.go",
        "volume_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/python/guest-init/cmd",
    visibility = ["//visibility:private"],
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

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = ["main_test.go"],
    embed = [":cmd_lib"],
)
