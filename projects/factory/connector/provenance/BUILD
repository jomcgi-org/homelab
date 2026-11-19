load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "provenance",
    srcs = [
        "provenance.go",
        "reviewer.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/provenance",
    visibility = ["//visibility:public"],
    deps = ["//projects/factory/connector/itsaplan"],
)

go_test(
    name = "provenance_test",
    srcs = ["provenance_test.go"],
    embed = [":provenance"],
    deps = ["//projects/factory/connector/itsaplan"],
)
