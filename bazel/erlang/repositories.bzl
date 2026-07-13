"""Prebuilt Erlang/OTP for the EmberVM control-plane build.

Unlike bazel/ocaml (which builds its compiler from source because a prebuilt
linked a too-new glibc), the RBE executor here was probed to be Ubuntu 22.04.5
(glibc 2.35). hex.pm publishes OTP builds compiled *for* ubuntu-22.04, so the
matching prebuilt runs natively on the executor and its crypto app links the
executor's already-present runtime libssl.so.3 (pulled in by git/curl) with no
dev headers needed. This sidesteps the from-source OTP build + OpenSSL/ncurses
provisioning entirely.

The prebuilt is BUILD-time only: it runs mix/elixir to compile the control-plane
app to architecture-independent .beam bytecode. The release is built with
include_erts: false, and the apko runtime image supplies erlang-27 per-arch from
Wolfi, so the deployed control plane never uses this build-host OTP.

Elixir itself is architecture-independent .beam and is fetched separately (its
precompiled release only needs a working erl to run).
"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

# hex.pm's OTP build extracts to OTP-<ver>/ with erts-*/, lib/, and an Install
# script that bakes ERL_ROOT paths (run `Install -minimal <abs-root>` before use).
_OTP_BUILD = """
filegroup(
    name = "otp",
    srcs = glob(["**"], exclude = ["BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel"]),
    visibility = ["//visibility:public"],
)

exports_files(["Install"], visibility = ["//visibility:public"])
"""

# Elixir ships a precompiled release (bin/ + lib/, .beam bytecode) that is
# architecture-independent and only needs a working erl to run. So one Elixir
# archive serves every arch; it is fetched, not built.
_ELIXIR_BUILD = """
filegroup(
    name = "elixir",
    srcs = glob(["**"], exclude = ["BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel"]),
    visibility = ["//visibility:public"],
)

# bin/elixir anchors the Elixir root for staging (its dir's parent is the root).
exports_files(["bin/elixir"], visibility = ["//visibility:public"])
"""

def _erlang_impl(_ctx):
    http_archive(
        # OTP version MUST match the apko image's Wolfi erlang-27 (27.3.4.2)
        # exactly: an include_erts:false release pins exact OTP app versions in
        # its .boot, so a build/runtime patch mismatch fails the pod at boot. If
        # Wolfi's erlang-27 bumps, re-pin both this and the image package together.
        name = "otp_ubuntu2204_amd64",
        urls = ["https://builds.hex.pm/builds/otp/ubuntu-22.04/OTP-27.3.4.2.tar.gz"],
        sha256 = "32c3ee239855556350f9700cf942a0a70b60228a277f314397a709e992345dfc",
        strip_prefix = "OTP-27.3.4.2",
        build_file_content = _OTP_BUILD,
    )
    http_archive(
        name = "elixir_1_18_4",
        urls = ["https://github.com/elixir-lang/elixir/releases/download/v1.18.4/elixir-otp-27.zip"],
        sha256 = "5be18f35e329f7c5914a80dd9f323d7bbb144616df1ed16f6f0862a1900b4bb5",
        build_file_content = _ELIXIR_BUILD,
    )

erlang = module_extension(
    implementation = _erlang_impl,
    doc = "Fetches the prebuilt ubuntu-22.04 OTP 27 (@otp_ubuntu2204_amd64) and precompiled Elixir 1.18.4 (@elixir_1_18_4).",
)
