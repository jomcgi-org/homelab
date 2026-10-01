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
    srcs = [
        "s6_tlc_helpers_test.go",
        "s6_tlc_test.go",
    ],
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

# The TLC toolchain for S6 (issue #6415), shipped in the image at fixed paths:
# the JRE at /opt/tla/jre (java finds its lib/ relative to bin/java), the jar
# at /opt/tla/tla2tools.jar and the spec at /opt/tla/specs/adoption_trace.tla.
# Inert unless the chart renders S6_TLC_JAVA, S6_TLC_JAR and S6_TLC_SPEC_DIR
# (conformance.s6.enabled, false in every environment). Moving any path means
# updating the chart's S6_TLC_* env in the same change. Every DT_NEEDED of the
# JRE's bin/java and the libraries TLC loads resolves from glibc and ld-linux
# already in apko.lock.json; only the GUI and sound libraries (libawt_xawt,
# libsplashscreen, libjsound) need X11 or ALSA, and headless TLC never loads
# them. Owner, modes and symlink handling: tla_layer_mtree.sh.
genrule(
    name = "tla_mtree",
    srcs = [
        "//projects/embervm/specs:adoption_trace.tla",
        "@temurin21_jre_linux_amd64//:bin/java",
        "@temurin21_jre_linux_amd64//:jre",
        "@tla2tools//file",
    ],
    outs = ["tla_mtree.txt"],
    cmd = "bash $(execpath tla_layer_mtree.sh) " +
          "$(execpath @temurin21_jre_linux_amd64//:bin/java) " +
          "$(execpath @tla2tools//file) " +
          "$(execpath //projects/embervm/specs:adoption_trace.tla) " +
          "$(execpaths @temurin21_jre_linux_amd64//:jre) > $@",
    tools = ["tla_layer_mtree.sh"],
)

tar(
    name = "tla_tar_amd64",
    srcs = [
        "//projects/embervm/specs:adoption_trace.tla",
        "@temurin21_jre_linux_amd64//:jre",
        "@tla2tools//file",
    ],
    mtree = ":tla_mtree",
)

# Shipped-layer proof (issue #6415): extracts :tla_tar_amd64, checks the
# /opt/tla paths, owner and modes, then runs runS6 against the extracted
# java, jar and spec dir, so the bytes the image carries are the bytes that
# pass and fail windows. Mirrors s6_tlc_test's platform gate and timeout.
go_test(
    name = "s6_tlc_image_test",
    timeout = "moderate",
    srcs = [
        "s6_tlc_helpers_test.go",
        "s6_tlc_image_test.go",
    ],
    data = [":tla_tar_amd64"],
    embed = [":conformance_lib"],
    env = {
        "S6_TLC_TEST_LAYER": "$(rlocationpath :tla_tar_amd64)",
    },
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_conformance_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/conformance",
    tars = [
        ":conformance_tar_amd64",
        ":tla_tar_amd64",
    ],
)
