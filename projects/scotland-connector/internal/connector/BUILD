load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "connector",
    srcs = [
        "aurora.go",
        "check.go",
        "connector.go",
        "light.go",
        "plan.go",
        "trip.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/connector",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/astro",
        "//projects/scotland-connector/internal/aurora",
        "//projects/scotland-connector/internal/stars",
        "//projects/scotland-connector/internal/verdict",
        "//projects/scotland-connector/internal/walks",
    ],
)

go_test(
    name = "connector_test",
    srcs = [
        "bench_test.go",
        "connector_test.go",
        "diversity_test.go",
        "light_test.go",
        "light_solver_test.go",
    ],
    embed = [":connector"],
    deps = [
        "//projects/scotland-connector/internal/astro",
        "//projects/scotland-connector/internal/fixture",
        "//projects/scotland-connector/internal/stars",
        "//projects/scotland-connector/internal/verdict",
        "//projects/scotland-connector/internal/walks",
    ],
)
