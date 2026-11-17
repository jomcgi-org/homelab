# The site data snapshot (HOME-209): copies each section's source objects
# from R2 into the site before a deploy. Hand-maintained.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_image_layer", "js_test")

js_binary(
    name = "snapshot",
    data = [
        "snapshot.mjs",
        "sources.json",
    ],
    entry_point = "main.mjs",
)

js_image_layer(
    name = "layers",
    binary = ":snapshot",
    root = "/app",
    visibility = ["//projects/jomcgi-site:__pkg__"],
)

js_test(
    name = "snapshot_test",
    data = [
        "snapshot.mjs",
        "sources.json",
    ],
    entry_point = "snapshot.test.mjs",
)
