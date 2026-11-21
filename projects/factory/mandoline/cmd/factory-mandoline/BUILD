load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@rules_go//go:def.bzl", "go_binary", "go_library")

# Both packages install the same command, with different compiled analysis scopes.
# gazelle:ignore

go_library(
    name = "factory-mandoline_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/mandoline/cmd/factory-mandoline",
    visibility = ["//visibility:private"],
    deps = ["//projects/factory/mandoline"],
)

go_binary(
    name = "factory-mandoline",
    embed = [":factory-mandoline_lib"],
    pure = "on",
)

go_binary(
    name = "factory-mandoline-audit",
    embed = [":factory-mandoline_lib"],
    pure = "on",
    x_defs = {"main.auditOnly": "true"},
)

tar(
    name = "diff_layer",
    srcs = [":factory-mandoline"],
    mtree = ["./usr/local/bin/factory-mandoline type=file mode=0755 content=$(execpath :factory-mandoline)"],
    visibility = ["//projects/factory/harness/codex:__pkg__"],
)

tar(
    name = "audit_layer",
    srcs = [":factory-mandoline-audit"],
    mtree = ["./usr/local/bin/factory-mandoline type=file mode=0755 content=$(execpath :factory-mandoline-audit)"],
    visibility = ["//projects/factory/harness/claude:__pkg__"],
)
