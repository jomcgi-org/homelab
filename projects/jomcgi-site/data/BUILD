# The site data snapshot and release fence (HOME-209): copies each section's
# source objects from R2 into the site before a deploy, and keeps an older
# deploy from replacing a newer release. Hand-maintained.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_image_layer", "js_library", "js_test")

js_library(
    name = "hikes_freshness",
    srcs = [
        "hikes-freshness.d.mts",
        "hikes-freshness.mjs",
    ],
    visibility = ["//projects/jomcgi-site/web:__pkg__"],
)

js_library(
    name = "stars_summary",
    srcs = [
        "stars-summary.d.mts",
        "stars-summary.mjs",
    ],
    visibility = ["//projects/jomcgi-site/web:__pkg__"],
)

js_library(
    name = "hikes_page_src",
    srcs = [
        "hikes-page.mjs",
        "snapshot.mjs",
    ],
    visibility = ["//projects/jomcgi-site/web:__pkg__"],
    deps = [
        ":hikes_freshness",
        ":stars_summary",
    ],
)

js_library(
    name = "snapshot_inputs",
    srcs = [
        "snapshot.mjs",
        "sources.json",
    ],
    visibility = ["//projects/jomcgi-site/web:__pkg__"],
    deps = [":stars_summary"],
)

js_binary(
    name = "snapshot",
    data = [
        "fence.mjs",
        "hikes-page.mjs",
        "snapshot.mjs",
        "sources.json",
        ":hikes_freshness",
        ":stars_summary",
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
        ":stars_summary",
    ],
    entry_point = "snapshot.test.mjs",
)

js_test(
    name = "hikes_page_test",
    data = [
        "hikes-page.mjs",
        "snapshot.mjs",
        ":hikes_freshness",
        ":stars_summary",
    ],
    entry_point = "hikes-page.test.mjs",
)

js_test(
    name = "fence_test",
    data = ["fence.mjs"],
    entry_point = "fence.test.mjs",
)
