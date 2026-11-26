load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_go//go:def.bzl", "go_binary", "go_library")

# gazelle:ignore

go_library(
    name = "kagent-hermes_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/harness/hermes/cmd/kagent-hermes",
    visibility = ["//visibility:private"],
    deps = ["//projects/factory/harness/hermes/adapter"],
)

go_binary(
    name = "kagent-hermes",
    embed = [":kagent-hermes_lib"],
    pure = "on",
    visibility = ["//projects/factory/harness/hermes:__pkg__"],
)

platform_transition_filegroup(
    name = "kagent_hermes_amd64",
    srcs = [":kagent-hermes"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

# The adapter and the durable /data directory it writes HERMES_HOME into.
tar(
    name = "layer",
    srcs = [":kagent_hermes_amd64"],
    mtree = [
        "./usr/local/bin/kagent-hermes type=file content=$(execpath :kagent_hermes_amd64) mode=0555 uid=0 gid=0",
        "./data type=dir mode=0755 uid=65532 gid=65532",
    ],
    visibility = ["//projects/factory/harness/hermes:__pkg__"],
)
