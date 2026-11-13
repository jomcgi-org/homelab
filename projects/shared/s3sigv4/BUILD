load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "s3sigv4",
    srcs = ["sigv4.go"],
    importpath = "github.com/jomcgi/homelab/projects/shared/s3sigv4",
    visibility = [
        "//projects/embervm/noded/store:__pkg__",
        "//projects/scotland-connector/internal/r2:__pkg__",
    ],
)

go_test(
    name = "sigv4_test",
    srcs = ["sigv4_test.go"],
    embed = [":s3sigv4"],
)

go_test(
    name = "s3sigv4_test",
    srcs = ["sigv4_test.go"],
    embed = [":s3sigv4"],
)
