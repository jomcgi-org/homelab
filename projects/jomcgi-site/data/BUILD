# The site data snapshot and release fence (HOME-209): copies each section's
# source objects from R2 into the site before a deploy, and keeps an older
# deploy from replacing a newer release. Hand-maintained.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_image_layer", "js_test")

js_binary(
    name = "snapshot",
    data = [
        "fence.mjs",
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

js_test(
    name = "fence_test",
    data = ["fence.mjs"],
    entry_point = "fence.test.mjs",
)
