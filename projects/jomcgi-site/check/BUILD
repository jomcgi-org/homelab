# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_image_layer", "js_library", "js_test")
load("@npm//:defs.bzl", "npm_link_all_packages")

npm_link_all_packages(name = "node_modules")

js_library(
    name = "src",
    srcs = [
        "browser.mjs",
        "fixture.mjs",
        "freshness.json",
        "headers.mjs",
        "release-fixture.mjs",
        "scope.mjs",
        "smoke.mjs",
        "tier1.mjs",
    ],
    deps = [
        ":node_modules/@sparticuz/chromium",
        ":node_modules/axe-core",
        ":node_modules/parse5",
        ":node_modules/playwright-core",
        ":node_modules/undici",
        "//projects/jomcgi-site/data:snapshot_inputs",
        "//projects/jomcgi-site/edge:migration_routes",
        "//projects/jomcgi-site/web:budget_lib",
        "//projects/jomcgi-site/web:headers",
    ],
)

js_binary(
    name = "site-check",
    data = [":src"],
    entry_point = "main.mjs",
)

js_image_layer(
    name = "layers",
    binary = ":site-check",
    root = "/app",
    visibility = ["//projects/jomcgi-site:__pkg__"],
)

js_test(
    name = "tier1_test",
    data = [
        "fixture.mjs",
        ":src",
        "//projects/jomcgi-site/web:headers",
        "//projects/jomcgi-site/web:redirects",
    ],
    entry_point = "tier1.test.mjs",
)

js_test(
    name = "browser_test",
    size = "medium",
    data = [
        "fixture.mjs",
        ":src",
        "//projects/jomcgi-site/web:headers",
    ],
    entry_point = "browser.test.mjs",
)

js_test(
    name = "unpack_test",
    size = "medium",
    data = [
        "fixture.mjs",
        ":src",
        "//projects/jomcgi-site/web:headers",
    ],
    entry_point = "unpack.test.mjs",
)

js_test(
    name = "deployed_test",
    size = "medium",
    data = [
        "fixture.mjs",
        ":src",
        "//projects/jomcgi-site/web:build",
        "//projects/jomcgi-site/web:headers",
    ],
    entry_point = "deployed.test.mjs",
    env = {"SITE_CHECK_DIST": "$(rootpath //projects/jomcgi-site/web:build)"},
)
