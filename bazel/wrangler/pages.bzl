"""Bazel rules for Cloudflare Pages deployment.

This module provides rules to deploy built frontend assets to Cloudflare Pages
using wrangler CLI. The rules handle Bazel runfiles resolution and support
authentication via CLOUDFLARE_API_TOKEN environment variable.

Example usage:
    load("//bazel/wrangler:defs.bzl", "wrangler_pages")
    load("@npm//:wrangler/package_json.bzl", wrangler_bin = "bin")

    # Create wrangler binary target (required before using macro)
    wrangler_bin.wrangler_binary(name = "wrangler")

    wrangler_pages(
        name = "my_site",
        dist = ":build_dist",
        project_name = "my-cloudflare-project",
        wrangler = ":wrangler",
    )

Deploy with: bazel run //path/to:my_site.push
"""

load("@bazel_skylib//lib:paths.bzl", "paths")

def _rlocationpath(file, workspace_name):
    """Convert file path to rlocation path for runfiles."""
    if file.short_path.startswith("../"):
        return file.short_path[3:]
    return paths.join(workspace_name, file.short_path)

def _wrangler_pages_push_impl(ctx):
    """Implementation for wrangler_pages_push rule."""

    # Get wrangler binary
    wrangler_bin = ctx.executable.wrangler

    # The dist can be a directory (from js_run_binary out_dirs) or a filegroup
    dist_files = ctx.files.dist
    if len(dist_files) == 1 and dist_files[0].is_directory:
        dist_path = _rlocationpath(dist_files[0], ctx.workspace_name)
    else:
        # For filegroups, we need the common parent directory
        # Assume the first file's dirname is the dist root
        if not dist_files:
            fail("dist must contain at least one file")
        dist_path = _rlocationpath(dist_files[0], ctx.workspace_name).rsplit("/", 1)[0]

    # Create the push script
    push_script = ctx.actions.declare_file(ctx.label.name + ".bash")

    ctx.actions.expand_template(
        template = ctx.file._template,
        output = push_script,
        is_executable = True,
        substitutions = {
            "{{WRANGLER}}": _rlocationpath(wrangler_bin, ctx.workspace_name),
            "{{DIST_DIR}}": dist_path,
            "{{PROJECT_NAME}}": ctx.attr.project_name,
            "{{BRANCH}}": ctx.attr.branch,
        },
    )

    # Collect runfiles - include dist files and wrangler with its dependencies
    runfiles = ctx.runfiles(files = dist_files)
    runfiles = runfiles.merge(ctx.attr.wrangler[DefaultInfo].default_runfiles)
    runfiles = runfiles.merge(ctx.attr._runfiles[DefaultInfo].default_runfiles)

    return [DefaultInfo(
        executable = push_script,
        runfiles = runfiles,
    )]

wrangler_pages_push = rule(
    implementation = _wrangler_pages_push_impl,
    doc = "Push built assets to Cloudflare Pages using wrangler CLI.",
    attrs = {
        "dist": attr.label(
            doc = "Label to the built dist directory or filegroup containing the assets to deploy.",
            mandatory = True,
            allow_files = True,
        ),
        "project_name": attr.string(
            doc = "Cloudflare Pages project name (as shown in the Cloudflare dashboard).",
            mandatory = True,
        ),
        "branch": attr.string(
            doc = "Git branch name for the deployment. If empty, wrangler auto-detects from git.",
            default = "",
        ),
        "wrangler": attr.label(
            doc = "Wrangler CLI binary target. Create with wrangler_bin.wrangler_binary(name = 'wrangler').",
            mandatory = True,
            executable = True,
            cfg = "exec",
        ),
        "_template": attr.label(
            doc = "Shell script template for the push command.",
            default = "//bazel/wrangler:pages_push.sh.tpl",
            allow_single_file = True,
        ),
        "_runfiles": attr.label(
            doc = "Bazel runfiles library for script execution.",
            default = "@bazel_tools//tools/bash/runfiles",
        ),
    },
    executable = True,
)

def wrangler_pages(name, dist, project_name, wrangler, branch = "", visibility = None):
    """High-level macro for Cloudflare Pages deployment.

    Creates a .push target that deploys the dist directory to Cloudflare Pages.

    The consuming BUILD file must create the wrangler binary target first:
        load("@npm//:wrangler/package_json.bzl", wrangler_bin = "bin")
        wrangler_bin.wrangler_binary(name = "wrangler")

    Args:
        name: Base name for the generated targets.
        dist: Label to the built dist directory (e.g., from vite_build).
        project_name: Cloudflare Pages project name.
        wrangler: Label to the wrangler binary target.
        branch: Optional git branch for deployment preview URLs.
        visibility: Visibility of the generated targets.

    Creates:
        :{name}.push - Executable target to deploy to Cloudflare Pages.

    Example:
        load("@npm//:wrangler/package_json.bzl", wrangler_bin = "bin")

        wrangler_bin.wrangler_binary(name = "wrangler")

        wrangler_pages(
            name = "mysite",
            dist = ":build_dist",
            project_name = "mysite-jomcgi-dev",
            wrangler = ":wrangler",
        )

        # Deploy with: bazel run //projects/<service>/frontend:mysite.push
    """
    wrangler_pages_push(
        name = name + ".push",
        dist = dist,
        project_name = project_name,
        wrangler = wrangler,
        branch = branch,
        visibility = visibility,
    )
