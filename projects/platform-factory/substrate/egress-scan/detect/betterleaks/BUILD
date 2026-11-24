load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "betterleaks",
    srcs = ["betterleaks.go"],
    embedsrcs = ["extra_rules.toml"],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect/betterleaks",
    visibility = ["//projects/platform-factory/substrate/egress-scan:__subpackages__"],
    deps = [
        "//projects/platform-factory/substrate/egress-scan/detect",
        "@com_github_betterleaks_betterleaks_v2//config",
        "@com_github_betterleaks_betterleaks_v2//report",
        "@com_github_betterleaks_betterleaks_v2//scan",
        "@com_github_betterleaks_betterleaks_v2//sources",
    ],
)

go_test(
    name = "betterleaks_test",
    srcs = ["betterleaks_test.go"],
    embed = [":betterleaks"],
    deps = [
        "//projects/platform-factory/substrate/egress-scan/detect",
        "//projects/platform-factory/substrate/egress-scan/detect/detecttest",
    ],
)
