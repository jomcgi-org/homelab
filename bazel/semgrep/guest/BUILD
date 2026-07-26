# gazelle:ignore
#
# Tar layers for the semgrep-guest microVM image:
#   - engine_tar:  the offline-Pro scan-server engine — the proprietary binary at
#                  /opt/semgrep/osemgrep-pro (invoked via argv[0] dispatch) plus
#                  the OSS /opt/semgrep/semgrep-core it shells out to. amd64 only.
#   - rules_tar:   the merged rule set (local rules + the 5 Pro packs) flattened
#                  under /etc/semgrep/rules/, structure-preserved to avoid the
#                  basename collisions that exist across the local rule subdirs.
#                  The scan-server reads this via SEMGREP_SCAN_RULES.
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files", "strip_prefix")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")

package(default_visibility = ["//visibility:public"])

# --- Offline-Pro engine layer (amd64 only) --------------------------------------
#
# The engine is a prebuilt OCI artifact from PRIVATE GHCR (fetched with GHCR_TOKEN
# in CI). It roots two binaries which MUST be co-located: semgrep-core-proprietary
# (placed as osemgrep-pro so `osemgrep-pro mcp --experimental --pro` selects the
# OCaml scan-server) and semgrep-core (the OSS core the proprietary engine shells
# to). apko_image's multiarch_tars uses the _amd64 / _arm64 suffixes.
# The proprietary engine (osemgrep-pro) plus the "installed" layout its `scan --pro`
# CLI insists on: a semgrep-core-proprietary binary next to semgrep-core and a
# pro-installed-by.txt version stamp (matching the engine's own version, since the
# engine, so its `scan --pro` runs whole-tree interfile analysis) refuses unless
# the Pro engine looks INSTALLED. Verified from the guest image's own pysemgrep:
# it resolves semgrep-core to its BUNDLED copy, so it reads the version stamp at
# site-packages/semgrep/bin/pro-installed-by.txt (NOT /usr/bin or /opt/semgrep),
# checks it equals pysemgrep's __VERSION__, and resolves the pro binary via PATH
# as semgrep-core-proprietary. We bake exactly that: the stamp at the bundled-core
# dir set to the pysemgrep version, and a /usr/bin/semgrep-core-proprietary symlink
# to the engine. pro-installed-by.txt MUST match the apko `semgrep` package's
# pysemgrep version (currently 1.165.0); it is independent of the digest-pinned
# ENGINE version (1.161.0), which pysemgrep drives fine across that drift.
tar(
    name = "engine_tar_amd64",
    srcs = [
        ":pro-installed-by.txt",
        "@semgrep_experimental_engine_amd64//:osemgrep_pro",
        "@semgrep_experimental_engine_amd64//:semgrep_core",
    ],
    mtree = [
        "./opt/semgrep/osemgrep-pro type=file content=$(execpath @semgrep_experimental_engine_amd64//:osemgrep_pro) mode=0755 uid=0 gid=0",
        "./opt/semgrep/semgrep-core type=file content=$(execpath @semgrep_experimental_engine_amd64//:semgrep_core) mode=0755 uid=0 gid=0",
        # The pro engine, resolved by pysemgrep via PATH as semgrep-core-proprietary.
        "./usr/bin/semgrep-core-proprietary type=link link=/opt/semgrep/osemgrep-pro uid=0 gid=0",
        # The version stamp, next to pysemgrep's bundled semgrep-core (where it reads it).
        "./opt/chainguard/semgrep/lib/python3.13/site-packages/semgrep/bin/pro-installed-by.txt type=file content=$(execpath :pro-installed-by.txt) mode=0644 uid=0 gid=0",
    ],
)

# arm64 is unsupported: there is no arm64 engine build and no arm64 Firecracker
# nodes. The shared apko_image macro always emits a two-platform index, so an
# arm64 image is still built and pushed — but it carries no engine (only an empty
# /opt/semgrep dir) and is never pulled (the substrate rootfs-builder pulls the
# amd64 manifest on node-4). Booting it would fail at scandriver.Start.
tar(
    name = "engine_tar_arm64",
    mtree = [
        "./opt/semgrep type=dir mode=0755 uid=0 gid=0",
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
# These guest rule packs use the frozen, manually managed semgrep_guest pins,
# rather than the auto-updated CI semgrep_pro digests. Namespace each under its
# own pro-<lang>/ subdir so the packs cannot collide with each other or with the
# local rules.
pkg_files(
    name = "pro_golang_files",
    srcs = ["@semgrep_guest_rules_golang//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-golang",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_files(
    name = "pro_python_files",
    srcs = ["@semgrep_guest_rules_python//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-python",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_files(
    name = "pro_javascript_files",
    srcs = ["@semgrep_guest_rules_javascript//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-javascript",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_files(
    name = "pro_kubernetes_files",
    srcs = ["@semgrep_guest_rules_kubernetes//:rules"],
    attributes = pkg_attributes(mode = "0644"),
    prefix = "etc/semgrep/rules/pro-kubernetes",
    strip_prefix = strip_prefix.from_pkg(""),
)

pkg_files(
    name = "pro_rust_files",
    srcs = ["@semgrep_guest_rules_rust//:rules"],
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
