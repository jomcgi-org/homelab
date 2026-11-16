load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "itsaplan",
    srcs = [
        "client.go",
        "human_gate.go",
        "renovate.go",
        "review.go",
        "status.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/itsaplan",
    visibility = ["//visibility:public"],
)

go_test(
    name = "itsaplan_test",
    srcs = [
        "client_test.go",
        "review_test.go",
        "status_test.go",
    ],
    embed = [":itsaplan"],
)
