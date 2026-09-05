"""Module extension for Semgrep OSS engine OCI artifacts.

Creates repository rules for the semgrep-core binary (per-platform).
Reads digest pins from digests.bzl. Reuses oci_archive from semgrep_pro.
"""

load("//bazel/semgrep/third_party/semgrep_pro:oci_archive.bzl", "oci_archive")
load(":digests.bzl", "SEMGREP_DIGESTS")

_GHCR_PREFIX = "jomcgi/homelab/tools/semgrep/engine"

_ENGINE_BUILD = """\
filegroup(
    name = "engine",
    srcs = glob(["semgrep-core", "libs/**"]),
    visibility = ["//visibility:public"],
)
"""

def _semgrep_impl(module_ctx):
    # Linux engines (from PyPI manylinux wheels)
    for platform in ["amd64", "arm64"]:
        oci_archive(
            name = "semgrep_engine_" + platform,
            image = _GHCR_PREFIX + "-" + platform,
            digest = SEMGREP_DIGESTS.get("engine_" + platform, ""),
            build_file_content = _ENGINE_BUILD,
        )

    # macOS engines (from PyPI macOS wheels)
    for platform in ["osx_arm64", "osx_x86_64"]:
        oci_archive(
            name = "semgrep_engine_" + platform,
            image = _GHCR_PREFIX + "-" + platform.replace("_", "-"),
            digest = SEMGREP_DIGESTS.get("engine_" + platform, ""),
            build_file_content = _ENGINE_BUILD,
        )

semgrep = module_extension(implementation = _semgrep_impl)
