# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

go_library(
    name = "conformance_lib",
    srcs = [
        "main.go",
        "s6_trace.go",
        "scenarios.go",
        "verdict.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/conformance",
    visibility = ["//visibility:private"],
)

go_binary(
    name = "conformance",
    embed = [":conformance_lib"],
    visibility = ["//visibility:private"],
)

go_test(
    name = "conformance_test",
    srcs = [
        "s6_trace_test.go",
        "scenarios_test.go",
        "verdict_test.go",
    ],
    embed = [":conformance_lib"],
)

# S6 end-to-end TLC proof (issue #6415): drives runS6 against an httptest
# trace window with the pinned toolchain and asserts pass, fail naming the
# invariant, and vacuous off real TLC output. Separate from conformance_test
# because only this target stages the JRE, the jar and the spec, and only it
# is Linux-gated. Hand-maintained: this package carries gazelle:ignore.
# Linux x86_64 only: the Temurin JRE is a linux-amd64 runtime, so other
# platforms skip this target while CI's Linux executor still runs it.
go_test(
    name = "s6_tlc_test",
    timeout = "moderate",
    srcs = ["s6_tlc_test.go"],
    data = [
        "//projects/embervm/specs:adoption_trace.tla",
        "@temurin21_jre_linux_amd64//:bin/java",
        "@temurin21_jre_linux_amd64//:jre",
        "@tla2tools//file",
    ],
    embed = [":conformance_lib"],
    env = {
        "S6_TLC_TEST_JAR": "$(rlocationpath @tla2tools//file)",
        "S6_TLC_TEST_JAVA": "$(rlocationpath @temurin21_jre_linux_amd64//:bin/java)",
        "S6_TLC_TEST_SPEC": "$(rlocationpath //projects/embervm/specs:adoption_trace.tla)",
    },
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)

platform_transition_filegroup(
    name = "conformance_amd64_bin",
    srcs = [":conformance"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "conformance_tar_amd64",
    srcs = [":conformance_amd64_bin"],
    mtree = ["./usr/local/bin/conformance type=file content=$(execpath :conformance_amd64_bin) mode=0755 uid=0 gid=0"],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_conformance_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/conformance",
    tars = [":conformance_tar_amd64"],
)
