# Hand-maintained rules_js app, independent of the monolith workspace package.
# gazelle:ignore
# gazelle:exclude src
load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@npm//projects/factory-landing/web:typescript/package_json.bzl", typescript_bin = "bin")
load("@npm//projects/factory-landing/web:vite/package_json.bzl", vite_bin = "bin")
load("@npm//projects/factory-landing/web:vitest/package_json.bzl", vitest_bin = "bin")

npm_link_all_packages(name = "node_modules")

js_library(
    name = "src",
    srcs = glob(
        ["src/**"],
        exclude = [
            "src/**/*.test.tsx",
            "src/setup.ts",
        ],
    ) + [
        "index.html",
        "package.json",
        "tsconfig.json",
        "vite.config.ts",
    ],
    deps = [
        ":node_modules/@astryxdesign/core",
        ":node_modules/@astryxdesign/theme-neutral",
        ":node_modules/@esbuild/linux-x64",
        ":node_modules/@rollup/rollup-linux-x64-gnu",
        ":node_modules/@stylexjs/stylex",
        ":node_modules/react",
        ":node_modules/react-dom",
        ":node_modules/vite",
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
    visibility = ["//projects/factory-landing:__pkg__"],
)

build_test(
    name = "build_test",
    targets = [":build"],
)

js_library(
    name = "test_src",
    srcs = glob(["src/**/*.test.tsx"]) + [
        "src/setup.ts",
        "vitest.config.ts",
    ],
    deps = [
        ":node_modules/@testing-library/dom",
        ":node_modules/@testing-library/react",
        ":node_modules/@types/react",
        ":node_modules/@types/react-dom",
        ":node_modules/happy-dom",
        ":node_modules/vitest",
        ":src",
    ],
)

vitest_bin.vitest_test(
    name = "test",
    args = ["run"],
    chdir = package_name(),
    data = [":test_src"],
)

typescript_bin.tsc_test(
    name = "typecheck",
    args = ["--noEmit"],
    chdir = package_name(),
    data = [":test_src"],
)
