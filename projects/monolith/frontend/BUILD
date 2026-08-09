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
        ":node_modules/@homelab/design-system",
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
        # Client-side mermaid diagram rendering on the public docs pages
        ":node_modules/mermaid",
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
        # Terminal emulator for the private k8s demo tab (client-only dynamic
        # import; the package must still be linked for vite to resolve it)
        ":node_modules/@xterm/xterm",
        ":node_modules/@xterm/addon-fit",
    ],
)

js_run_binary(
    name = "build",
    srcs = [":src"],
    args = ["build"],
    chdir = package_name(),
    # Out dir is "dist", not "build": svelte.config.js defaults there so a
    # local vite build cannot collide with this BUILD file on macOS's
    # case-insensitive filesystem. build_tar remaps it to /app/build.
    out_dirs = ["dist"],
    tool = ":vite",
    visibility = ["//projects/monolith:__pkg__"],
)

# Public-only source tree (ADR 004 Phase 5d code-absence): identical to :src but
# additionally excludes the authenticated route tree (src/routes/private/**) and
# private-only components (src/lib/private/**). SvelteKit is filesystem-routed,
# so omitting these files from the source set drops those pages, their SSR
# handlers, and their client chunks entirely from the build.
js_library(
    name = "src_public",
    srcs = glob(
        [
            "src/**/*",
            "static/**/*",
        ],
        allow_empty = True,
        exclude = [
            "src/**/*.test.js",
            "src/routes/private/**",
            "src/lib/private/**",
        ],
    ) + [
        "package.json",
        "svelte.config.js",
        "vite.config.js",
    ],
    deps = [
        ":node_modules/@homelab/design-system",
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
        # Client-side mermaid diagram rendering on the public docs pages
        ":node_modules/mermaid",
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
    ],
)

js_run_binary(
    name = "build_public",
    srcs = [":src_public"],
    args = ["build"],
    chdir = package_name(),
    env = {"SVELTE_OUT_DIR": "build_public"},
    # Distinct out dir (svelte.config reads SVELTE_OUT_DIR) so this action does
    # not collide with :build's "build" output in the same package.
    # build_tar_public remaps it to /app/build at image time, so the runtime
    # path is unchanged.
    out_dirs = ["build_public"],
    tool = ":vite",
    visibility = ["//projects/monolith:__pkg__"],
)

# Validate the frontend builds in CI without going through py3_image's
# platform transition (which breaks native npm packages like esbuild/rollup).
build_test(
    name = "build_test",
    targets = [":build"],
)

build_test(
    name = "build_test_public",
    targets = [":build_public"],
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

genrule(
    name = "build_tar_public",
    srcs = [":build_public"],
    outs = ["build_tar_public.tar"],
    cmd = """
        set -euo pipefail
        mkdir -p tmp/app/build
        cp -rL $(location :build_public)/* tmp/app/build/
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
        # Resolved by the $env/dynamic/private alias in vitest.config.js; same
        # rationale: lives outside src/ so it never reaches the app bundle.
        "test/env-dynamic-private-stub.js",
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

# Public-only frontend image (ADR 004 Phase 5d). Reuses the same apko.yaml
# (node build/index.js on PORT 3000) but ships the private-route-free build, so
# the image literally does not contain the authenticated page code.
apko_image(
    name = "image_public",
    config = "//projects/monolith/frontend:apko.yaml",
    contents = "@monolith_frontend_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/public-frontend",
    tars = [":build_tar_public"],
)
