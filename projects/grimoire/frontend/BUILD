"Grimoire frontend - Vite-based React + TypeScript app"

load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@npm//projects/grimoire/frontend:vite/package_json.bzl", vite_bin = "bin")
load("//bazel/tools/oci:apko_nginx_frontend.bzl", "apko_nginx_frontend")

npm_link_all_packages(name = "node_modules")

vite_bin.vite_binary(name = "vite")

js_library(
    name = "src",
    srcs = glob(
        [
            "src/**/*",
        ],
        exclude = [
            "src/**/*.test.*",
        ],
    ) + [
        "index.html",
        "package.json",
        "tsconfig.json",
        "vite.config.ts",
    ],
    deps = [
        ":node_modules/@tanstack/react-query",
        ":node_modules/@tanstack/react-router",
        ":node_modules/@vitejs/plugin-react",
        ":node_modules/react",
        ":node_modules/react-dom",
        ":node_modules/vite",
        ":node_modules/zustand",
    ],
)

js_run_binary(
    name = "build",
    srcs = [":src"],
    args = ["build"],
    chdir = package_name(),
    out_dirs = ["dist"],
    tool = ":vite",
)

filegroup(
    name = "dist",
    srcs = [":build"],
    visibility = ["//projects/grimoire/deploy:__subpackages__"],
)

apko_nginx_frontend(
    name = "image",
    config = "apko.yaml",
    contents = "@grimoire_frontend_lock//:contents",
    dist = ":build",
    repository = "ghcr.io/jomcgi/homelab/projects/grimoire/frontend",
)
