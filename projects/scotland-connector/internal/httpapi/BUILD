load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "httpapi",
    srcs = [
        "aurora.go",
        "httpapi.go",
    ],
    embedsrcs = [
        "method.md",
        "openapi.json",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/httpapi",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/meter",
    ],
)

go_test(
    name = "httpapi_test",
    srcs = ["httpapi_test.go"],
    embed = [":httpapi"],
    deps = [
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/fixture",
        "//projects/scotland-connector/internal/meter",
        "//projects/scotland-connector/internal/verdict",
        "@com_github_prometheus_client_golang//prometheus",
    ],
)

# method.md is served by the API and also indexed with the repo docs (#6446).
# See //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = ["method.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
