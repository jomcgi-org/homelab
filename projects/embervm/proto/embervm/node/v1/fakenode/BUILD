# Fake NodeService server for the cross-language round-trip test. Hand-managed
# (it depends on the sibling :nodev1 go_library built from generated srcs, which
# lives in a gazelle-ignored package), so gazelle leaves this directory alone.
# gazelle:ignore

load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "fakenode_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1/fakenode",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/embervm/proto/embervm/node/v1:nodev1",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//status",
    ],
)

go_binary(
    name = "fakenode",
    embed = [":fakenode_lib"],
    visibility = [
        "//projects/embervm:__subpackages__",
        # The Elixir test lane stages this binary so the restart-wedge scenario
        # (#4761) can drive a real Dispatcher against a fault-injectable node.
        # Without it the scenario skips, and a harness proof that never runs is
        # the defect class the harness exists to catch.
        "//bazel/erlang:__pkg__",
    ],
)
