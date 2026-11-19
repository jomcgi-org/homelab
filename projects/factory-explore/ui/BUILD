# Hand-maintained rules_js app, independent of the monolith workspace package.
# gazelle:ignore
# gazelle:exclude src
load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@npm//projects/factory-explore/ui:vite/package_json.bzl", vite_bin = "bin")
load("@npm//projects/factory-explore/ui:vitest/package_json.bzl", vitest_bin = "bin")

exports_files(
    ["public/kubectl-worker.js"],
    visibility = ["//projects/factory-explore/kubectl-wasm:__pkg__"],
)

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
        ":node_modules/@xterm/xterm",
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

js_library(
    name = "test_src",
    srcs = ["session.test.ts"],
    deps = [
        ":node_modules/happy-dom",
        ":node_modules/vitest",
        ":src",
    ],
)

vitest_bin.vitest_test(
    name = "test",
    args = [
        "run",
        "--mode",
        "production",
    ],
    chdir = package_name(),
    data = [":test_src"],
)
