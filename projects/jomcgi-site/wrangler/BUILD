# The pinned wrangler CLI and its node_modules, packaged as image layers for the
# deploy Job. Hand-maintained; gazelle does not manage rules_js packages here.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_image_layer")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@npm//projects/jomcgi-site/wrangler:wrangler/package_json.bzl", wrangler_bin = "bin")

npm_link_all_packages(name = "node_modules")

wrangler_bin.wrangler_binary(name = "wrangler")

js_image_layer(
    name = "layers",
    binary = ":wrangler",
    root = "/app",
    visibility = ["//projects/jomcgi-site:__pkg__"],
)
