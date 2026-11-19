load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = [
        "cache.go",
        "main.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory-explore/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "@com_github_go_jose_go_jose_v4//:go-jose",
        "@com_github_go_jose_go_jose_v4//jwt",
        "@org_golang_x_time//rate",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = [
        "main_test.go",
        "verifier_test.go",
    ],
    embed = [":cmd_lib"],
    deps = [
        "@com_github_go_jose_go_jose_v4//:go-jose",
        "@com_github_go_jose_go_jose_v4//jwt",
        "@org_golang_x_time//rate",
    ],
)
