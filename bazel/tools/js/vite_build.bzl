"""Unified Vite build macro for React/frontend projects.

This macro standardizes frontend builds across the repository, providing
a consistent interface for Vite-based projects (React, Vue, etc.) and
Astro projects which use Vite internally.

Example usage:
    load("//bazel/tools/js:vite_build.bzl", "vite_build")
    load("@npm//projects/my_app:vite/package_json.bzl", vite_bin = "bin")

    # Create vite binary target (required before using macro)
    vite_bin.vite_binary(name = "vite")

    vite_build(
        name = "build",
        srcs = glob(["src/**/*", "public/**/*"]) + ["index.html", "package.json"],
        tool = ":vite",
        config = "vite.config.js",
        deps = [
            "react",
            "react-dom",
            "@vitejs/plugin-react",
        ],
        visibility = ["//projects/my_app:__pkg__"],
    )
"""

load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@npm//:defs.bzl", "npm_link_all_packages")

def vite_build(
        name,
        srcs,
        tool,
        config = "vite.config.js",
        deps = [],
        bazel_deps = [],
        out_dir = "dist",
        build_args = None,
        visibility = None):
    """Standard Vite/Astro build for frontend projects.

    Creates several targets:
      - :node_modules - Linked npm packages from pnpm workspace
      - :src - Source js_library containing all source files
      - :{name} - Build output from vite/astro build
      - :{name}_dist - Filegroup exposing built dist for downstream consumption

    The consuming BUILD file must create the build tool binary target first:
        load("@npm//projects/my_app:vite/package_json.bzl", vite_bin = "bin")
        vite_bin.vite_binary(name = "vite")

    Args:
        name: Name of the build target.
        srcs: Source files for the build (glob patterns recommended).
            Should include index.html, package.json, and all src/public files.
        tool: Label of the vite/astro binary target (e.g., ":vite" or ":astro").
            Must be created by the consuming BUILD file using package_json.bzl.
        config: Vite/Astro config file (default: vite.config.js).
            Use astro.config.mjs for Astro projects. Set to None if config
            is already included in srcs.
        deps: List of npm package names to include as dependencies.
            These are linked from the workspace's node_modules.
            Example: ["react", "react-dom", "@vitejs/plugin-react"]
        bazel_deps: List of Bazel target labels to include as dependencies.
            Use for cross-package deps like shared CSS.
            Example: ["//projects/my_app/shared:css"]
        out_dir: Output directory name (default: "dist").
        build_args: Custom build arguments (default: ["build"]).
        visibility: Visibility for the dist filegroup.
    """

    # Link npm packages for this workspace package (skip if already defined)
    if not native.existing_rule("node_modules"):
        npm_link_all_packages(name = "node_modules")

    # Default build args
    args = build_args if build_args != None else ["build"]

    # Collect all config files
    config_files = [config] if config else []

    # Build node_modules dependency labels
    node_modules_deps = [":node_modules/" + dep for dep in deps]

    # Source library containing all files needed for the build
    js_library(
        name = "src",
        srcs = srcs + config_files,
        deps = node_modules_deps + bazel_deps,
    )

    # Run vite/astro build to produce the dist directory
    js_run_binary(
        name = name,
        srcs = [":src"],
        args = args,
        chdir = native.package_name(),
        out_dirs = [out_dir],
        tool = tool,
    )

    # Expose the built dist for downstream consumption (container packaging, etc.)
    native.filegroup(
        name = name + "_dist",
        srcs = [":" + name],
        visibility = visibility,
    )
