# gazelle:ignore
load("@rules_go//go:def.bzl", "go_binary")

go_binary(
    name = "dev-smoke",
    srcs = ["main_linux.go"],
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//projects/embervm/noded/fcvm/driver",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/noded/vsockhttp",
    ],
)
