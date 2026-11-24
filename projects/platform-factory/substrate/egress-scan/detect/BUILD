load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "detect",
    srcs = [
        "detect.go",
        "doc.go",
        "host.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect",
    visibility = ["//projects/platform-factory/substrate/egress-scan:__subpackages__"],
)

go_test(
    name = "detect_test",
    srcs = ["detect_test.go"],
    embed = [":detect"],
)
