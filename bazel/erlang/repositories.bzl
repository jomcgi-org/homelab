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

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive", "http_file")

# Hex dependency tarballs, fetched hermetically at repo-fetch time (the host has
# network; the RBE executor does not). Hex tarballs are a nested format (an outer
# tar holding contents.tar.gz), which http_archive cannot unpack, so each is an
# http_file and bazel/erlang/mix_test.sh|mix_release.sh unpack the inner archive
# into the project's deps/ tree. The control-plane mix.exs then consumes them as
# `path:` deps (Path SCM), so mix never contacts hex.pm: no mix.lock, no .hex
# markers, no `mix deps.get`, no Hex archive install. This is the hex-dependency
# analog of the prebuilt-OTP de-risk.
#
# The closure below is exqlite (the SQLite op-log driver) plus everything it
# declares non-optionally: db_connection -> telemetry, and the build-time
# elixir_make + cc_precompiler. sha256 is the outer tarball hash (verified with
# `shasum -a 256 <name>-<version>.tar` against repo.hex.pm). When bumping a
# version, re-fetch the tarball and re-pin the sha here. (table is exqlite's only
# optional dep and is not pulled.)
_HEX_DEPS = [
    ("exqlite", "0.38.0", "f3da7b6e7b08bd548c33a118890d0eb8c5395fe093b31c8b329663234d0e988e"),
    ("db_connection", "2.10.2", "510b14482330f1af6490a2fa0efd8d4f1435d1529b165647df22ac0f2df0fa93"),
    ("elixir_make", "0.9.0", "db23d4fd8b757462ad02f8aa73431a426fe6671c80b200d9710caf3d1dd0ffdb"),
    ("cc_precompiler", "0.1.11", "3427232caf0835f94680e5bcf082408a70b48ad68a5f5c0b02a3bea9f3a075b9"),
    ("telemetry", "1.4.2", "928f6495066506077862c0d1646609eed891a4326bee3126ba54b60af61febb1"),
]

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
    for name, version, sha in _HEX_DEPS:
        http_file(
            name = "hex_%s" % name,
            urls = ["https://repo.hex.pm/tarballs/%s-%s.tar" % (name, version)],
            sha256 = sha,
            # Keep the <name>-<version>.tar filename: the staging scripts derive
            # the deps/<name>/ directory from it.
            downloaded_file_path = "%s-%s.tar" % (name, version),
        )

    # The Hex package manager archive. Elixir's precompiled release does not bundle
    # Hex, but mix needs the Hex SCM *registered* to even parse the hex-style dep
    # declarations inside our path deps' own mix.exs files (e.g. exqlite declaring
    # `{:db_connection, "~> 2.1"}`); without it mix aborts with "Could not find an
    # SCM for dependency". The mix drivers `mix archive.install` this offline so no
    # actual hex.pm fetch ever happens (the path overrides keep resolution local).
    # This is the exact build mix itself installs for Elixir 1.18 (installs/1.18.0/
    # hex.ez is an alias to the current Hex for that series); the pinned sha256
    # makes a silent alias rotation fail the cold fetch loudly instead of drifting.
    http_file(
        name = "hex_archive",
        urls = ["https://builds.hex.pm/installs/1.18.0/hex.ez"],
        sha256 = "55ea0adcd1adf5d26db47fcc69b365af98cd8afc06c78434c29db73b45758a28",
        downloaded_file_path = "hex.ez",
    )

erlang = module_extension(
    implementation = _erlang_impl,
    doc = "Fetches the prebuilt ubuntu-22.04 OTP 27 (@otp_ubuntu2204_amd64), precompiled Elixir 1.18.4 (@elixir_1_18_4), and the control-plane hex dependency tarballs (@hex_*).",
)
