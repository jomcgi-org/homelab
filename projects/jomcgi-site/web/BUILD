# Hand-maintained rules_js app, independent of the monolith workspace package.
# gazelle:ignore
# gazelle:exclude src
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_library", "js_run_binary", "js_test")
load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@npm//projects/jomcgi-site/web:typescript/package_json.bzl", typescript_bin = "bin")
load("@npm//projects/jomcgi-site/web:vitest/package_json.bzl", vitest_bin = "bin")

npm_link_all_packages(name = "node_modules")

js_library(
    name = "src",
    # Every HTML file, so the page-registry test sees each one in the sandbox.
    srcs = glob(
        [
            "**/*.html",
            "public/**",
            "src/**",
        ],
        exclude = [
            "dist/**",
            "node_modules/**",
            "src/**/*.test.tsx",
            "src/setup.ts",
        ],
    ) + [
        "build.mjs",
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

# Vite build, then each page's server-rendered HTML written into its entry.
js_binary(
    name = "build_site",
    data = [":src"],
    entry_point = "build.mjs",
)

js_run_binary(
    name = "build",
    srcs = [":src"],
    chdir = package_name(),
    out_dirs = ["dist"],
    tool = ":build_site",
    visibility = ["//projects/jomcgi-site:__pkg__"],
)

build_test(
    name = "build_test",
    targets = [":build"],
)

# Fails when a page's initial JS is over the 150 KiB gzipped budget.
js_test(
    name = "budget_test",
    args = ["$(rootpath :build)"],
    data = [":build"],
    entry_point = "budget.mjs",
)

# Fails when a built page's root is empty or still holds its marker.
js_test(
    name = "prerendered_test",
    args = ["$(rootpath :build)"],
    data = [
        "budget.mjs",
        ":build",
    ],
    entry_point = "prerendered.mjs",
)

js_library(
    name = "test_src",
    srcs = glob(["src/**/*.test.tsx"]) + [
        "budget.d.mts",
        "budget.mjs",
        "prerendered.d.mts",
        "prerendered.mjs",
        "src/setup.ts",
        "vitest.config.ts",
    ],
    deps = [
        ":node_modules/@testing-library/dom",
        ":node_modules/@testing-library/react",
        ":node_modules/@types/react",
        ":node_modules/@types/react-dom",
        ":node_modules/axe-core",
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
    include_transitive_types = True,
)
