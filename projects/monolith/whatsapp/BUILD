load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_go//go:def.bzl", "go_library", "go_test")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Go BUILD rules are hand-written here: gazelle's Go language does not generate
# under projects/monolith (it is a Python module tree). Keep srcs/deps in sync by
# hand when adding files, as with the other monolith Go targets.
go_library(
    name = "whatsapp",
    srcs = [
        "client.go",
        "config.go",
        "forward.go",
        "health.go",
        "outbox.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/monolith/whatsapp",
    visibility = ["//visibility:public"],
    deps = [
        "@com_github_jackc_pgx_v5//stdlib:go_default_library",
        "@fi_mau_go_whatsmeow//:go_default_library",
        "@fi_mau_go_whatsmeow//proto/waE2E:go_default_library",
        "@fi_mau_go_whatsmeow//store/sqlstore:go_default_library",
        "@fi_mau_go_whatsmeow//types:go_default_library",
        "@fi_mau_go_whatsmeow//types/events:go_default_library",
        "@fi_mau_go_whatsmeow//util/log:go_default_library",
    ],
)

go_test(
    name = "whatsapp_test",
    srcs = [
        "client_test.go",
        "config_test.go",
        "forward_test.go",
        "outbox_test.go",
    ],
    embed = [":whatsapp"],
    deps = [
        "@fi_mau_go_whatsmeow//:go_default_library",
        "@fi_mau_go_whatsmeow//proto/waCommon:go_default_library",
        "@fi_mau_go_whatsmeow//proto/waE2E:go_default_library",
        "@fi_mau_go_whatsmeow//types:go_default_library",
        "@fi_mau_go_whatsmeow//types/events:go_default_library",
    ],
)

# WhatsApp channel gateway container image (ADR 039). Built as an apko image
# (Wolfi base + ca-certificates) rather than a distroless go_image so it carries
# a CA bundle for TLS to WhatsApp's servers and Postgres. The Go binary is
# cross-compiled per arch and layered at /usr/local/bin/whatsapp (the apko
# entrypoint). apko appends _amd64/_arm64 to the base per arch.

platform_transition_filegroup(
    name = "whatsapp_amd64_bin",
    srcs = ["//projects/monolith/whatsapp/cmd:whatsapp"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "whatsapp_tar_amd64",
    srcs = [":whatsapp_amd64_bin"],
    mtree = [
        "./usr/local/bin/whatsapp type=file content=$(execpath :whatsapp_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

platform_transition_filegroup(
    name = "whatsapp_arm64_bin",
    srcs = ["//projects/monolith/whatsapp/cmd:whatsapp"],
    target_platform = "@rules_go//go/toolchain:linux_arm64",
)

tar(
    name = "whatsapp_tar_arm64",
    srcs = [":whatsapp_arm64_bin"],
    mtree = [
        "./usr/local/bin/whatsapp type=file content=$(execpath :whatsapp_arm64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@whatsapp_lock//:contents",
    multiarch_tars = [":whatsapp_tar"],
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/whatsapp",
)
