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
            "src/factory/test-fixtures.ts",
            "src/setup.ts",
        ],
    ) + [
        "blog-content.d.mts",
        "blog-content.mjs",
        "build.mjs",
        "factory-fixtures.ts",
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
        ":node_modules/@sveltejs/vite-plugin-svelte",
        ":node_modules/marked",
        ":node_modules/react",
        ":node_modules/react-dom",
        ":node_modules/svelte",
        ":node_modules/vite",
        "//projects/factory-storyboard:web_schema",
        "//projects/monolith/frontend:blog_sources",
        "//projects/monolith/frontend:leaderboard_sources",
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

# Requires the Factory entry and caps each page's initial JS at 150 KiB gzipped.
js_test(
    name = "budget_test",
    args = [
        "$(rootpath :build)",
        "factory/index.html",
    ],
    data = [":build"],
    entry_point = "budget.mjs",
)

js_test(
    name = "factory_built_test",
    args = ["$(rootpath :build)"],
    data = [
        ":build",
        ":node_modules/happy-dom",
    ],
    entry_point = "factory-built.test.mjs",
)

# Fails on an empty root, a missing post, or missing manifest titles/sections.
js_test(
    name = "prerendered_test",
    args = ["$(rootpath :build)"],
    data = [
        "budget.mjs",
        ":build",
        ":node_modules/happy-dom",
        "//projects/monolith/frontend:blog_sources",
    ],
    entry_point = "prerendered.mjs",
)

js_test(
    name = "marketplace_test",
    args = ["$(rootpath :build)"],
    data = [
        ":build",
        ":hub_fixture",
    ],
    entry_point = "marketplace.test.mjs",
)

js_library(
    name = "hub_fixture",
    srcs = ["hub-fixture.mjs"],
    deps = ["//projects/monolith/frontend:scotland_migration_sources"],
)

# Checks the emitted pages and routes, not just the source React trees.
js_test(
    name = "scotland_built_test",
    args = ["$(rootpath :build)"],
    data = [
        "budget.mjs",
        ":build",
        ":hub_fixture",
        ":node_modules/axe-core",
        ":node_modules/happy-dom",
        "//projects/jomcgi-site/edge:migration_routes",
    ],
    entry_point = "scotland-built.test.mjs",
)

# Check the built leaderboard and its deferred snapshot/code boundary.
js_test(
    name = "leaderboard_built_test",
    args = ["$(rootpath :build)"],
    data = [
        "budget.mjs",
        ":build",
        ":node_modules/axe-core",
        ":node_modules/happy-dom",
        ":src",
    ],
    entry_point = "leaderboard-built.mjs",
)

js_library(
    name = "test_src",
    srcs = glob(["src/**/*.test.tsx"]) + [
        "budget.d.mts",
        "budget.mjs",
        "prerendered.d.mts",
        "prerendered.mjs",
        "src/factory/test-fixtures.ts",
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
        "//projects/factory-storyboard:web_fixtures",
        "//projects/monolith/frontend:scotland_migration_sources",
        "//projects/monolith/frontend:leaderboard_migration_sources",
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
