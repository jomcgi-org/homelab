load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:go_image.bzl", "go_image")

go_library(
    name = "api_lib",
    srcs = [
        "campaign.go",
        "character.go",
        "dice.go",
        "encounter.go",
        "feed.go",
        "firestore.go",
        "main.go",
        "middleware.go",
        "session.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/grimoire/api",
    visibility = ["//visibility:private"],
    deps = [
        "@com_google_cloud_go_firestore//:firestore",
        "@org_golang_google_api//iterator",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//status",
    ],
)

go_test(
    name = "api_test",
    srcs = [
        "campaign_test.go",
        "character_test.go",
        "dice_test.go",
        "encounter_test.go",
        "feed_test.go",
        "firestore_test.go",
        "main_test.go",
        "middleware_test.go",
        "session_test.go",
    ],
    embed = [":api_lib"],
)

go_binary(
    name = "api",
    embed = [":api_lib"],
    visibility = ["//visibility:public"],
)

go_image(
    name = "image",
    binary = ":api",
    repository = "ghcr.io/jomcgi/homelab/projects/grimoire/api",
)
