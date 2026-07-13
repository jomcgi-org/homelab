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

def _erlang_impl(_ctx):
    http_archive(
        name = "otp_ubuntu2204_amd64",
        urls = ["https://builds.hex.pm/builds/otp/ubuntu-22.04/OTP-27.3.4.14.tar.gz"],
        sha256 = "0045c32b7c41b1924d68c9d51958aab609a09447a79298c42bd87bdded0827e7",
        strip_prefix = "OTP-27.3.4.14",
        build_file_content = _OTP_BUILD,
    )

erlang = module_extension(
    implementation = _erlang_impl,
    doc = "Fetches the prebuilt ubuntu-22.04 OTP 27 tarball as @otp_ubuntu2204_amd64.",
)
