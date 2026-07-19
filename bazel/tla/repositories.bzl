"""Prebuilt TLC model checker for the EmberVM TLA+ pilot (ADR embervm/006).

TLC (the TLA+ model checker, shipped as tla2tools.jar) checks the adoption
protocol spec in `projects/embervm/specs/` during CI. It is a build-time-only
correctness checker: nothing deployed uses Java, and the checker's outputs
(counterexample traces, "no error" verdicts) are architecture-independent, so a
single linux-amd64 toolchain is by design, the same posture as the prebuilt
`@protoc_linux_x86_64` codegen tool in bazel/erlang.

Both artifacts are fetched at repo-fetch time (the host has network; the RBE
executor is network-less) and run natively on the Ubuntu 22.04 x86_64 executor:

  - tla2tools.jar (a plain jar of bytecode) is fetched as an http_file. It
    bundles both entry points the pilot needs: tlc2.TLC (the model checker) and
    pcal.trans (the PlusCal-to-TLA+ translator that the tlc.sh driver runs for
    its translation-freshness check). No separate PlusCal install is required.

    We pin v1.7.4 (the newest tagged NON-prerelease that ships tla2tools.jar),
    NOT the newer-looking v1.8.0. v1.8.0 ("The Clarke release") is a GitHub
    prerelease whose tla2tools.jar asset is rebuilt in place on the same tag
    (its manifest Build-TimeStamp advances daily), so its sha is a moving target
    that would break a hermetic sha-pinned fetch the next time it is rebuilt.
    v1.7.4's asset is immutable (published 2024-08, class timestamps 2024-08-08),
    which is what a pinned fetch needs. TLC + PlusCal are stable across this gap;
    the pilot uses no 1.8-only feature.

  - The Temurin 21 JRE (not JDK: TLC only runs bytecode, it never compiles) is
    fetched as an http_archive. This repo has no rules_java and no hermetic JDK
    toolchain, so rather than provision one, we vendor a prebuilt JRE tarball the
    same way bazel/erlang vendors prebuilt OTP: the linux x64 hotspot build runs
    natively on the amd64 executor with no extra system packages.

sha256 is the exact bytes fetched from each URL. The JRE sha matches Adoptium's
published checksum for jdk-21.0.11+10 (verified against the api.adoptium.net
package checksum). When bumping either artifact, re-fetch and re-pin here.
"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive", "http_file")

# The Temurin JRE tarball extracts to jdk-<ver>-jre/ with bin/, lib/, etc. The
# glob exposes the whole runtime; bin/java is the entry point the genrules anchor
# on (its dir's parent is the JRE root, mirroring bazel/erlang's bin/elixir
# anchor pattern).
_JRE_BUILD = """
filegroup(
    name = "jre",
    srcs = glob(["**"], exclude = ["BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel"]),
    visibility = ["//visibility:public"],
)

exports_files(["bin/java"], visibility = ["//visibility:public"])
"""

def _tla_impl(_ctx):
    http_file(
        name = "tla2tools",
        urls = ["https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"],
        sha256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88",
        downloaded_file_path = "tla2tools.jar",
    )
    http_archive(
        name = "temurin21_jre_linux_amd64",
        urls = ["https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.11%2B10/OpenJDK21U-jre_x64_linux_hotspot_21.0.11_10.tar.gz"],
        sha256 = "e5038aae3ca9ff670bc696496b0728dbd23d280026bad30291cb919221ecfdcb",
        strip_prefix = "jdk-21.0.11+10-jre",
        build_file_content = _JRE_BUILD,
    )

tla = module_extension(
    implementation = _tla_impl,
    doc = "Prebuilt TLC model checker (@tla2tools) and the Temurin 21 JRE (@temurin21_jre_linux_amd64) that runs it on the linux-amd64 RBE executor. Build-time only; nothing deployed uses Java.",
)
