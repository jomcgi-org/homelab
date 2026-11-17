load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "store",
    srcs = [
        "admission.go",
        "deferred.go",
        "delivery.go",
        "human_gate.go",
        "notify.go",
        "recovery.go",
        "renovate.go",
        "sessions.go",
        "status.go",
        "store.go",
        "transcript.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/store",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/connector/itsaplan",
        "@com_github_lib_pq//:pq",
    ],
)

go_test(
    name = "store_test",
    srcs = [
        "deferred_test.go",
        "recovery_test.go",
        "renovate_test.go",
        "status_test.go",
        "store_test.go",
        "transcript_test.go",
    ],
    data = ["@postgres_test//:postgres"],
    deps = [
        ":store",
        "//projects/factory/connector/internal/pgtest",
        "//projects/factory/connector/migrations",
    ],
)
