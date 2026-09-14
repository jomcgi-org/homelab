# gazelle:ignore
load("@rules_go//go:def.bzl", "go_binary", "go_test")

go_binary(
    name = "dev-smoke",
    srcs = [
        "main_linux.go",
        "server_linux.go",
    ],
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//projects/embervm/noded/cmd/dev-smoke/synthetic",
        "//projects/embervm/noded/fcvm/driver",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/noded/vsockhttp",
    ],
)

go_test(
    name = "dev-smoke_test",
    srcs = [
        "server_linux.go",
        "server_linux_test.go",
    ],
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//projects/embervm/noded/cmd/dev-smoke/synthetic",
        "//projects/embervm/noded/fcvm/driver",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/noded/vsockhttp",
    ],
)
