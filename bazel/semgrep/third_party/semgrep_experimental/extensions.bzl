"""Module extension for the experimental offline-Pro Semgrep engine.

Vendors the osemgrep-pro warm scan-server engine image as an OCI artifact from
private GHCR (amd64 only). Reuses the shared oci_archive repository rule from the
semgrep_pro third_party module.

The engine image roots two native binaries (plus an empty libs/ dir):
  - semgrep-core-proprietary: the Pro engine. The guest invokes it AS
    osemgrep-pro (argv[0] dispatch) so `mcp --experimental --pro` selects the
    OCaml MCP scan-server rather than the Python one.
  - semgrep-core: the OSS core the proprietary engine shells out to. It MUST be
    co-located in the same directory as osemgrep-pro at runtime.

Both are exposed as separate filegroups so the guest image can place them
together under /opt/semgrep (see bazel/semgrep/guest/BUILD engine_tar_amd64).
"""

load("//bazel/semgrep/third_party/semgrep_pro:oci_archive.bzl", "oci_archive")
load(":digests.bzl", "SEMGREP_EXPERIMENTAL_DIGESTS")

_GHCR_PREFIX = "jomcgi/homelab/tools/semgrep-experimental"

_ENGINE_BUILD = """\
filegroup(
    name = "osemgrep_pro",
    srcs = ["semgrep-core-proprietary"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "semgrep_core",
    srcs = ["semgrep-core"],
    visibility = ["//visibility:public"],
)
"""

def _semgrep_experimental_impl(_module_ctx):
    oci_archive(
        name = "semgrep_experimental_engine_amd64",
        image = _GHCR_PREFIX + "/engine-amd64",
        digest = SEMGREP_EXPERIMENTAL_DIGESTS.get("engine_amd64", ""),
        build_file_content = _ENGINE_BUILD,
    )

semgrep_experimental = module_extension(implementation = _semgrep_experimental_impl)
