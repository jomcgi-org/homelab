load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "aoc_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/advent_of_code/cmd/aoc",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/advent_of_code/pkg/aoc",
        "//projects/advent_of_code/pkg/cookies",
        "@com_github_spf13_cobra//:cobra",
    ],
)

go_binary(
    name = "aoc",
    embed = [":aoc_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "aoc_test",
    srcs = ["main_test.go"],
    embed = [":aoc_lib"],
)
