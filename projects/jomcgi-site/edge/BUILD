# Hand-maintained rules_js launcher and tests, with no npm dependencies.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_image_layer", "js_library", "js_test")

js_library(
    name = "src",
    srcs = [
        "apply.mjs",
        "cache-settings.json",
        "imported/2026-11-17.json",
        "state.mjs",
    ],
)

js_binary(
    name = "apply",
    data = [":src"],
    entry_point = "cli.mjs",
)

js_image_layer(
    name = "layers",
    binary = ":apply",
    root = "/app",
    visibility = ["//projects/jomcgi-site:__pkg__"],
)

js_test(
    name = "apply_test",
    data = [":src"],
    entry_point = "apply.test.mjs",
)
