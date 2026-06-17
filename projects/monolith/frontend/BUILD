# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@npm//projects/monolith/frontend:vite/package_json.bzl", vite_bin = "bin")
load("@npm//projects/monolith/frontend:vitest/package_json.bzl", vitest_bin = "bin")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

npm_link_all_packages(name = "node_modules")

vite_bin.vite_binary(name = "vite")

js_library(
    name = "src",
    srcs = glob(
        [
            "src/**/*",
            "static/**/*",
        ],
        allow_empty = True,
        exclude = [
            "src/**/*.test.js",
        ],
    ) + [
        "package.json",
        "svelte.config.js",
        "vite.config.js",
    ],
    deps = [
        ":node_modules/@sveltejs/adapter-node",
        ":node_modules/@sveltejs/kit",
        ":node_modules/@sveltejs/vite-plugin-svelte",
        ":node_modules/mdsvex",
        ":node_modules/svelte",
        ":node_modules/vite",
        # Hand-drawn SVG rendering
        ":node_modules/roughjs",
        # DAG layout engine
        ":node_modules/@dagrejs/dagre",
        # Runtime markdown rendering
        ":node_modules/marked",
        # WebGL basemap for the /app/ships live vessel map
        ":node_modules/maplibre-gl",
        # OpenTelemetry browser tracing
        ":node_modules/@opentelemetry/api",
        ":node_modules/@opentelemetry/sdk-trace-web",
        ":node_modules/@opentelemetry/exporter-trace-otlp-http",
        ":node_modules/@opentelemetry/instrumentation",
        ":node_modules/@opentelemetry/instrumentation-document-load",
        ":node_modules/@opentelemetry/instrumentation-fetch",
        ":node_modules/@opentelemetry/resources",
        ":node_modules/@opentelemetry/semantic-conventions",
        # Platform-specific native binaries for Linux CI (loaded at runtime via require())
        ":node_modules/@rollup/rollup-linux-x64-gnu",
        ":node_modules/@esbuild/linux-x64",
        # d3 sub-packages used by the /notes graph view
        ":node_modules/d3-quadtree",
        ":node_modules/d3-selection",
        ":node_modules/d3-zoom",
        # Shared design tokens consumed via @import in lib/global.css.
        "//projects/websites/shared:css",
    ],
)

js_run_binary(
    name = "build",
    srcs = [":src"],
    args = ["build"],
    chdir = package_name(),
    out_dirs = ["build"],
    tool = ":vite",
    visibility = ["//projects/monolith:__pkg__"],
)

# Validate the frontend builds in CI without going through py3_image's
# platform transition (which breaks native npm packages like esbuild/rollup).
build_test(
    name = "build_test",
    targets = [":build"],
)

genrule(
    name = "build_tar",
    srcs = [":build"],
    outs = ["build_tar.tar"],
    cmd = """
        set -euo pipefail
        mkdir -p tmp/app/build
        cp -rL $(location :build)/* tmp/app/build/
        chown -R 65532:65532 tmp/app 2>/dev/null || true
        tar -C tmp --owner=65532 --group=65532 -cf $@ .
    """,
)

# Test library
js_library(
    name = "test_lib",
    srcs = glob(
        ["src/**/*.test.js"],
        allow_empty = True,
    ) + [
        "vitest.config.js",
        # Resolved by the $app/environment alias in vitest.config.js; lives
        # outside src/ so it never reaches the app bundle.
        "test/app-environment-stub.js",
    ],
    deps = [
        ":node_modules/vitest",
        ":src",
    ],
)

# Run vitest tests
vitest_bin.vitest_test(
    name = "test",
    args = ["run"],
    chdir = package_name(),
    data = [
        ":src",
        ":test_lib",
    ],
)

apko_image(
    name = "image",
    config = "//projects/monolith/frontend:apko.yaml",
    contents = "@monolith_frontend_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/frontend",
    tars = [":build_tar"],
)
