load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "fakes",
    testonly = True,
    srcs = [
        "idp.go",
        "openai.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/internal/fakes",
    visibility = ["//projects/factory/broker:__subpackages__"],
)
