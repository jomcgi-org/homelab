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
        # The token tests verify the README's measured contrast table.
        "README.md",
        "components/Breadcrumb.svelte",
        "components/Button.svelte",
        "components/Disclosure.svelte",
        "components/Field.svelte",
        "components/PageHeader.svelte",
        "components/Tabs.svelte",
        "components/index.js",
        "data-display/KeyValue.svelte",
        "data-display/Metric.svelte",
        "data-display/Panel.svelte",
        "data-display/Status.svelte",
        "data-display/contracts.js",
        "data-display/core.js",
        "data-display/fixtures.js",
        "data-display/format.js",
        "data-display/index.js",
        "package.json",
        "tokens/contract.css",
        "tokens/technical-drawing.css",
    ],
    visibility = [
        "//:__pkg__",
        "//projects/monolith/frontend:__pkg__",
    ],
    deps = ["//:node_modules/svelte"],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
