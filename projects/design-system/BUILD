# Hand-maintained: the js extension wants to add an npm_package target here,
# but consumers depend on the npm_link_all_packages link
# (:node_modules/@homelab/design-system), not on a package target, so the
# generated one is dead weight and shows up as permanent `ci regen` drift.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_library")
load("@npm//:defs.bzl", "npm_link_all_packages")

npm_link_all_packages(name = "node_modules")

js_library(
    name = "pkg",
    srcs = [
        "package.json",
        "tokens/contract.css",
    ],
    visibility = [
        "//:__pkg__",
        "//projects/monolith/frontend:__pkg__",
    ],
)
