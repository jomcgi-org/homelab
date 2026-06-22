# gazelle:ignore
#
# ADR tooling/010 de-risking spike. Isolated, throwaway target that proves the
# rules_playwright-vendored chromium launches and renders as a hermetic Bazel
# test action on the linux RBE executors (the substrate question), before the
# real frontend/visual capture is migrated off its runtime `playwright install`.
# Not part of any shipped image; deleted once the substrate is settled.
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@npm//projects/monolith/frontend/visual-spike:@playwright/test/package_json.bzl", playwright_bin = "bin")

npm_link_all_packages(name = "node_modules")

# No `chdir`: the $(rootpath) browser path is relative to the exec root, so
# changing cwd would break PLAYWRIGHT_BROWSERS_PATH. Playwright resolves the
# config's `testDir` relative to the -c config file location, so the spec is
# still discovered without chdir.
playwright_bin.playwright_test(
    name = "spike_test",
    args = [
        "test",
        "-c",
        "projects/monolith/frontend/visual-spike/playwright.config.mjs",
    ],
    data = [
        "playwright.config.mjs",
        "spike.spec.mjs",
        ":node_modules/@playwright/test",
        # Vendored browser binaries (fetched once into the Bazel cache by the
        # rules_playwright repo rule) instead of a runtime download. Both the
        # full chromium and the headless shell are supplied so Playwright finds
        # whichever the headless launch resolves to.
        "@playwright//:chromium",
        "@playwright//:chromium-headless-shell",
    ],
    env = {
        # Directory that CONTAINS the vendored browser folders; Playwright reads
        # this instead of ~/.cache/ms-playwright.
        "PLAYWRIGHT_BROWSERS_PATH": "$(rootpath @playwright//:chromium-headless-shell)/../",
    },
    # Disables Bazel's local sandbox so chromium can spawn its helper processes;
    # does not prevent remote (RBE) execution, which is what we want to exercise.
    tags = ["no-sandbox"],
)
