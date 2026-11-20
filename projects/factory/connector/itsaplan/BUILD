load("@rules_go//go:def.bzl", "go_library", "go_test")

exports_files(["delivered_issue_usage.sql"])

filegroup(
    name = "delivered_usage_report",
    srcs = [
        "delivered_issue_usage.sql",
        "import_run_usage.sql",
        "report-delivered-usage.sh",
        "//projects/factory/connector/store:export_run_usage.sql",
    ],
    visibility = ["//projects/factory/tests/itsaplan:__pkg__"],
)

go_library(
    name = "itsaplan",
    srcs = [
        "client.go",
        "decision.go",
        "deferred.go",
        "human_gate.go",
        "renovate.go",
        "review.go",
        "status.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/itsaplan",
    visibility = ["//visibility:public"],
)

go_test(
    name = "itsaplan_test",
    srcs = [
        "client_test.go",
        "decision_test.go",
        "deferred_test.go",
        "review_test.go",
        "status_test.go",
    ],
    embed = [":itsaplan"],
)
