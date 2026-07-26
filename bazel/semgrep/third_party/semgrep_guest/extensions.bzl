"""Module extension for Semgrep Pro rule packs baked into the guest image."""

load("//bazel/semgrep/third_party/semgrep_pro:oci_archive.bzl", "oci_archive")
load(":digests.bzl", "SEMGREP_GUEST_DIGESTS")

_GHCR_PREFIX = "jomcgi/homelab/tools/semgrep-pro"

_RULES_BUILD = """\
filegroup(
    name = "rules",
    srcs = glob(["*.yaml", "*.json"], allow_empty = True),
    visibility = ["//visibility:public"],
)
"""

def _semgrep_guest_impl(_module_ctx):
    for lang in ["golang", "python", "javascript", "kubernetes", "rust"]:
        oci_archive(
            name = "semgrep_guest_rules_" + lang,
            image = _GHCR_PREFIX + "/rules-" + lang,
            digest = SEMGREP_GUEST_DIGESTS.get("rules_" + lang, ""),
            build_file_content = _RULES_BUILD,
        )

semgrep_guest = module_extension(implementation = _semgrep_guest_impl)
