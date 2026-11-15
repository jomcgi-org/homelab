load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "migrations",
    srcs = ["migrations.go"],
    embedsrcs = [
        "20261114000000_dispatch_schema.sql",
        "20261114120000_delivery_schema.sql",
        "20261114230000_merge_group.sql",
        "20261115000000_delivery_reads.sql",
        "20261115120000_caller_identity.sql",
        "20261115180000_recovery.sql",
        "20261115200000_idle_rounds.sql",
        "20261115203000_transcript.sql",
        "20261115210000_transcript_bytes.sql",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/migrations",
    visibility = ["//visibility:public"],
)
