# Hand-maintained, like //projects/design-system: consumers depend on the
# npm_link_all_packages link (:node_modules/@homelab/ui) and on :pkg.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_library")
load("@npm//:defs.bzl", "npm_link_all_packages")

npm_link_all_packages(name = "node_modules")

js_library(
    name = "pkg",
    srcs = glob(["src/**"]) + ["package.json"],
    visibility = [
        "//:__pkg__",
        "//projects/jomcgi-site/web:__pkg__",
    ],
    deps = [":node_modules/react"],
)
