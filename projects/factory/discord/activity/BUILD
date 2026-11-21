load("@aspect_rules_js//js:defs.bzl", "js_test")
load("@npm//:defs.bzl", "npm_link_all_packages")

npm_link_all_packages(name = "node_modules")

js_test(
    name = "catalog_test",
    data = [
        "catalog.js",
        "package.json",
        ":node_modules/@astryxdesign/core",
        ":node_modules/react",
        ":node_modules/react-dom",
    ],
    entry_point = "catalog.test.js",
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//:__pkg__"],
)
