load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "migrations",
    srcs = ["migrations.go"],
    embedsrcs = ["20261115000000_gate_state.sql"],
    importpath = "github.com/jomcgi/homelab/projects/factory/reviewer/migrations",
    visibility = ["//visibility:public"],
)
