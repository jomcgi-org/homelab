# gazelle:ignore
load("@rules_go//go:def.bzl", "go_binary")

go_binary(
    name = "guest",
    srcs = ["main_linux.go"],
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//projects/embervm/noded/vsockproto",
        "@org_golang_x_sys//unix",
    ],
)
