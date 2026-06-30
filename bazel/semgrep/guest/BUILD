# gazelle:ignore
#
# Tar layers for the semgrep-guest microVM image (Task 1.1):
#   - engine_tar:  the per-arch Semgrep Pro engine at
#                  /opt/semgrep/semgrep-core-proprietary (SEMGREP_CORE_BIN).
#   - rules_tar:   the merged rule set (local rules + the 5 Pro packs) flattened
#                  under /etc/semgrep/rules/, structure-preserved to avoid the
#                  basename collisions that exist across the local rule subdirs.
#
# The OSS semgrep-core is NOT baked here: the pysemgrep CLI installed in the image
# (the Wolfi `semgrep` package) ships its own OSS core, and SEMGREP_CORE_BIN
# overrides it to the Pro engine for actual scanning. So only the Pro engine is
# layered.
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files", "strip_prefix")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")

package(default_visibility = ["//visibility:public"])

# --- Pro engine layer (arch-specific) -------------------------------------------
#
# The engine binaries are prebuilt per-arch OCI artifacts from PRIVATE GHCR (fetched
# with GHCR_TOKEN in CI), so they are referenced by their arch-specific repos
# directly rather than via the platform-selecting alias (the alias resolves on the
# build/exec platform, which would pick the wrong arch when cross-building the image).
# apko_image's multiarch_tars uses the _amd64 / _arm64 suffixes.
tar(
    name = "engine_tar_amd64",
    srcs = ["@semgrep_pro_engine_amd64//:engine"],
    mtree = [
        "./opt/semgrep/semgrep-core-proprietary type=file content=$(execpath @semgrep_pro_engine_amd64//:engine) mode=0755 uid=0 gid=0",
    ],
)

tar(
    name = "engine_tar_arm64",
    srcs = ["@semgrep_pro_engine_arm64//:engine"],
    mtree = [
        "./opt/semgrep/semgrep-core-proprietary type=file content=$(execpath @semgrep_pro_engine_arm64//:engine) mode=0755 uid=0 gid=0",
    ],
)

# --- Merged rules layer (arch-independent) --------------------------------------
#
# Local rules keep their per-language subdir (python/, golang/, ...): basenames such
# as no-hardcoded-k8s-service-url.yaml exist in more than one subdir, so a flat
# layout would collide. semgrep recurses a config directory, so a preserved tree is
# read in full.
pkg_files(
    name = "local_rule_files",
    srcs = ["//bazel/semgrep/rules:local_rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules",
    strip_prefix = strip_prefix.from_root("bazel/semgrep/rules"),
)

# Each Pro pack is one or more large YAML/JSON files at its external repo root.
# Namespace each under its own pro-<lang>/ subdir so the packs cannot collide with
# each other or with the local rules.
pkg_files(
    name = "pro_golang_files",
    srcs = ["@semgrep_pro_rules_golang//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-golang",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_files(
    name = "pro_python_files",
    srcs = ["@semgrep_pro_rules_python//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-python",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_files(
    name = "pro_javascript_files",
    srcs = ["@semgrep_pro_rules_javascript//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-javascript",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_files(
    name = "pro_kubernetes_files",
    srcs = ["@semgrep_pro_rules_kubernetes//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-kubernetes",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_files(
    name = "pro_rust_files",
    srcs = ["@semgrep_pro_rules_rust//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-rust",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_tar(
    name = "rules_tar",
    srcs = [
        ":local_rule_files",
        ":pro_golang_files",
        ":pro_javascript_files",
        ":pro_kubernetes_files",
        ":pro_python_files",
        ":pro_rust_files",
    ],
)
