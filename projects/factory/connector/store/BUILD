load("@rules_go//go:def.bzl", "go_library", "go_test")

exports_files(["export_run_usage.sql"])

go_library(
    name = "store",
    srcs = [
        "activation.go",
        "admission.go",
        "deferred.go",
        "delivery.go",
        "human_gate.go",
        "notify.go",
        "planner.go",
        "provenance.go",
        "recovery.go",
        "renovate.go",
        "sessions.go",
        "status.go",
        "store.go",
        "transcript.go",
        "usage.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/store",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/connector/internal/tracing",
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/planner",
        "//projects/factory/connector/provenance",
        "@com_github_lib_pq//:pq",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel_trace//:trace",
    ],
)

go_test(
    name = "store_test",
    srcs = [
        "deferred_test.go",
        "planner_test.go",
        "provenance_test.go",
        "recovery_test.go",
        "renovate_test.go",
        "reviewer_provenance_test.go",
        "status_test.go",
        "store_test.go",
        "storyboard_messages_test.go",
        "transcript_test.go",
        "usage_test.go",
    ],
    data = [
        "@postgres_test//:postgres",
    ],
    embedsrcs = [
        "delivered_issue_usage.sql",
        "planner_breaker_test.sql",
    ],
    deps = [
        ":store",
        "//projects/factory/broker/config",
        "//projects/factory/broker/usage",
        "//projects/factory/connector/github",
        "//projects/factory/connector/internal/pgtest",
        "//projects/factory/connector/migrations",
        "//projects/factory/connector/planner",
        "//projects/factory/connector/provenance",
    ],
)
