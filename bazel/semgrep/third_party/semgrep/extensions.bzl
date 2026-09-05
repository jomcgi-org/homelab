"""Module extension for Semgrep OSS engine OCI artifacts.

Creates repository rules for the semgrep-core binary (per-platform).
Reads digest pins from digests.bzl. Reuses oci_archive from semgrep_pro.
"""

load("//bazel/semgrep/third_party/semgrep_pro:oci_archive.bzl", "oci_archive")
load("//bazel/semgrep/third_party/semgrep_pro:stub_engine.bzl", "host_is_macos", "stub_engine")
load(":digests.bzl", "SEMGREP_DIGESTS")

_GHCR_PREFIX = "jomcgi/homelab/tools/semgrep/engine"

_ENGINE_BUILD = """\
filegroup(
    name = "engine",
    srcs = glob(["semgrep-core", "libs/**"], allow_empty = True),
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

    # macOS engines (from PyPI macOS wheels). On a non-macOS host these are
    # declared as network-free stubs so a //... query never fetches their OCI
    # manifests (#5121); the platform select never picks them there anyway.
    for platform in ["osx_arm64", "osx_x86_64"]:
        if host_is_macos(module_ctx):
            oci_archive(
                name = "semgrep_engine_" + platform,
                image = _GHCR_PREFIX + "-" + platform.replace("_", "-"),
                digest = SEMGREP_DIGESTS.get("engine_" + platform, ""),
                build_file_content = _ENGINE_BUILD,
            )
        else:
            stub_engine(
                name = "semgrep_engine_" + platform,
                build_file_content = _ENGINE_BUILD,
            )

semgrep = module_extension(implementation = _semgrep_impl)
