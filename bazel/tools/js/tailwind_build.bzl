"""Tailwind CSS build macro for processing CSS with Tailwind v4.

This macro provides a reusable interface for processing CSS files with
Tailwind CSS v4's standalone CLI. It can be used alongside esbuild builds
to handle CSS processing that esbuild cannot do natively (like Tailwind's
@import "tailwindcss" directive).

Example usage:
    load("//bazel/tools/js:tailwind_build.bzl", "tailwind_build")
    load("@npm//projects/my_app:@tailwindcss/cli/package_json.bzl", tailwind_bin = "bin")

    # Create Tailwind binary target
    tailwind_bin.tailwindcss_binary(name = "tailwindcss")

    tailwind_build(
        name = "css",
        src = "src/index.css",
        out = "dist/styles.css",
        tool = ":tailwindcss",
        srcs = glob(["src/**/*.jsx", "src/**/*.tsx"]),  # Content files for purging
        deps = [":node_modules/tailwindcss"],
    )
"""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")

def tailwind_build(
        name,
        src,
        out,
        tool,
        srcs = [],
        deps = [],
        minify = True,
        sourcemap = False,
        visibility = None):
    """Process CSS with Tailwind CSS v4 CLI.

    Args:
        name: Name of the build target.
        src: Input CSS file (e.g., "src/index.css").
        out: Output CSS file path (e.g., "dist/styles.css").
        tool: Label of the tailwindcss binary target.
        srcs: Source files that Tailwind should scan for class usage.
            Include all JSX/TSX files that use Tailwind classes.
        deps: Dependencies needed for CSS processing (e.g., node_modules/tailwindcss).
        minify: Whether to minify the output (default: True).
        sourcemap: Whether to generate source maps (default: False).
        visibility: Visibility of the generated target.
    """

    # Use relative paths from the package directory since we chdir there
    args = [
        "--input",
        src,
        "--output",
        out,
    ]

    if minify:
        args.append("--minify")

    if sourcemap:
        args.append("--map")

    js_run_binary(
        name = name,
        srcs = [src] + srcs + deps,
        args = args,
        chdir = native.package_name(),
        outs = [out],
        tool = tool,
        visibility = visibility,
    )
