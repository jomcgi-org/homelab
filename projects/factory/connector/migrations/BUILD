load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "migrations",
    srcs = ["migrations.go"],
    embedsrcs = [
        "20261114000000_dispatch_schema.sql",
        "20261114120000_delivery_schema.sql",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/migrations",
    visibility = ["//visibility:public"],
)
