# gazelle:ignore
#
# Hermetic Bazel-native visual regression (ADR tooling/010).
#  - apko_image visual_chromium: the CI-execution-only exec image (chromium libs
#    + fonts), published under the ci/ GHCR namespace.
#  - capture: a js_run_binary that boots the :build_public app + the mock seam and
#    screenshots the public pages with the rules_playwright-vendored chromium,
#    running inside visual_chromium via exec_properties. Output: the out/ PNG tree
#    + manifest.json.
#  - diff_bin: pixel-diffs two staged capture dirs (PR vs main). No committed
#    baselines; the workflow step supplies both renders. See diff.mjs.
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_library", "js_run_binary")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

apko_image(
    name = "visual_chromium",
    config = "apko.yaml",
    contents = "@visual_chromium_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/ci/visual-chromium",
)

npm_link_all_packages(name = "node_modules")

# Capture sources + their committed inputs. Reads (targets.json, fixtures) resolve
# from the runfiles; writes go to BAZEL_BINDIR (see capture.mjs).
js_library(
    name = "capture_srcs",
    srcs = [
        "capture.mjs",
        "mock-server.mjs",
        "serve.mjs",
        "targets.json",
    ] + glob(["fixtures/**"]),
    deps = [":node_modules/@playwright/test"],
)

js_binary(
    name = "capture_bin",
    data = [":capture_srcs"],
    entry_point = "capture.mjs",
)

# No `chdir`: $(rootpath)/$(location) below are exec-root relative, so changing
# cwd would break them; capture.mjs writes to $BAZEL_BINDIR/$CAPTURE_OUT_SUBDIR
# instead of a cwd-relative dir.
js_run_binary(
    name = "capture",
    srcs = [
        ":capture_srcs",
        "//projects/monolith/frontend:build_public",
        # Both browser builds: Playwright's default headless launch resolves to
        # chromium-headless-shell; full chromium is kept available too.
        "@playwright//:chromium",
        "@playwright//:chromium-headless-shell",
    ],
    env = {
        # Directory CONTAINING the vendored browser folders. execpath (not
        # rootpath): this js_run_binary runs from the execroot, and capture.mjs
        # absolutizes this execroot-relative value before launch.
        "PLAYWRIGHT_BROWSERS_PATH": "$(execpath @playwright//:chromium-headless-shell)/..",
        # The adapter-node app entry serve.mjs spawns (via process.execPath).
        "APP_ENTRY": "$(location //projects/monolith/frontend:build_public)/index.js",
        # Package-relative out_dirs path capture.mjs joins onto BAZEL_BINDIR.
        "CAPTURE_OUT_SUBDIR": "projects/monolith/frontend/visual/out",
    },
    # Run inside the chromium exec image (default RBE image lacks chromium libs).
    exec_properties = {
        "container-image": "docker://ghcr.io/jomcgi/homelab/ci/visual-chromium@sha256:f3b9524b0160226a5dc904decb9adc25cb6d8068a6a75c84e6b31335869cf67f",
    },
    out_dirs = ["out"],
    tags = ["no-sandbox"],
    tool = ":capture_bin",
)

# Pixel-diff binary (pixelmatch + pngjs); isChanged is unit-tested separately.
# There are NO committed baselines: the "Visual regression" workflow step renders
# the PR and main (in-place ref-switch), stages both PNG sets, and runs this with
# CAPTURE_DIR/BASELINE_DIR/OUT_DIR pointed at those staged dirs (see diff.mjs).
# It is not a js_run_binary because its two inputs come from two separate Bazel
# invocations (PR tree + main tree), which cannot live in one action graph.
js_library(
    name = "diff_srcs",
    srcs = ["diff.mjs"],
    deps = [
        ":node_modules/pixelmatch",
        ":node_modules/pngjs",
    ],
)

js_binary(
    name = "diff_bin",
    data = [":diff_srcs"],
    entry_point = "diff.mjs",
)

# NOTE: deliberately NO build_test over :capture. Per ADR tooling/010 the capture
# runs only in the dedicated (non-gating, input-cached) "Visual regression"
# workflow step, keeping chromium out of the gating `bazel test //...` lane.
# Breakage surfaces in that step, like the Manifest diff check.
