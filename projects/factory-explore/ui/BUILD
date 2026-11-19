# Hand-maintained rules_js app, independent of the monolith workspace package.
# gazelle:ignore
# gazelle:exclude src
load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@npm//projects/factory-explore/ui:vite/package_json.bzl", vite_bin = "bin")

npm_link_all_packages(name = "node_modules")

js_library(
    name = "src",
    srcs = glob([
        "src/**",
        "public/**",
    ]) + [
        ".env.production",
        "index.html",
        "package.json",
        "vite.config.ts",
    ],
    deps = [
        ":node_modules/@esbuild/linux-x64",
        ":node_modules/@mdi/font",
        ":node_modules/@rollup/rollup-linux-x64-gnu",
        ":node_modules/@vitejs/plugin-vue",
        ":node_modules/oidc-client-ts",
        ":node_modules/vite",
        ":node_modules/vue",
        ":node_modules/vuetify",
    ],
)

vite_bin.vite_binary(name = "vite")

js_run_binary(
    name = "build",
    srcs = [":src"],
    args = ["build"],
    chdir = package_name(),
    out_dirs = ["dist"],
    tool = ":vite",
    visibility = ["//projects/factory-explore-site:__pkg__"],
)

build_test(
    name = "build_test",
    targets = [":build"],
)
