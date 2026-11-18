# The edge apply Job (HOME-192): makes zone jomcgi.dev's Workers routes and
# cache rules match routes.json and cache-rules.json. Hand-maintained.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_image_layer", "js_library", "js_test")

js_library(
    name = "migration_routes",
    srcs = ["routes.json"],
    visibility = [
        "//projects/jomcgi-site/check:__pkg__",
        "//projects/jomcgi-site/web:__pkg__",
    ],
)

js_binary(
    name = "apply",
    data = [
        "apply.mjs",
        "cache-rules.json",
        "routes.json",
    ],
    entry_point = "main.mjs",
)

js_image_layer(
    name = "layers",
    binary = ":apply",
    root = "/app",
    visibility = ["//projects/jomcgi-site:__pkg__"],
)

js_test(
    name = "apply_test",
    data = [
        "apply.mjs",
        "cache-rules.json",
        "main.mjs",
        "routes.json",
        "testdata/live-cache-rules-2026-11-17.json",
    ],
    entry_point = "apply.test.mjs",
)
