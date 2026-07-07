"""Bazel rules for packaging and pushing Helm charts to OCI registries."""

load("@bazel_skylib//lib:paths.bzl", "paths")

def _rlocationpath(file, workspace_name):
    """Convert file path to rlocation path for runfiles."""
    if file.short_path.startswith("../"):
        return file.short_path[3:]
    return paths.join(workspace_name, file.short_path)

# --- helm_package: build-time rule that produces a .tgz ---

def _helm_package_impl(ctx):
    """Package a Helm chart into a .tgz archive."""
    output = ctx.actions.declare_file(ctx.label.name + ".tgz")

    # Determine the chart directory from the first source file's path.
    # All srcs come from the same chart directory (via glob), so we can
    # derive the chart root from any file's root-relative path.
    first_src = ctx.files.srcs[0]
    chart_dir = first_src.dirname

    # Walk up to find the directory containing Chart.yaml
    # (files may be in subdirectories like templates/)
    for src in ctx.files.srcs:
        if src.basename == "Chart.yaml":
            chart_dir = src.dirname
            break

    # Build URL annotation patch command if url is specified.
    # This replaces org.opencontainers.image.url in Chart.yaml so GHCR
    # links to the chart's directory rather than the monorepo root.
    url_patch = ""
    if ctx.attr.url:
        url_patch = (
            "sed 's|org.opencontainers.image.url:.*|org.opencontainers.image.url: \"{url}\"|' " +
            "\"$WORK_DIR/Chart.yaml\" > \"$WORK_DIR/Chart.yaml.tmp\"\n" +
            "mv \"$WORK_DIR/Chart.yaml.tmp\" \"$WORK_DIR/Chart.yaml\""
        ).format(url = ctx.attr.url)

    values_overlay_merge = ""
    extra_inputs = list(ctx.files.srcs)
    if ctx.file.values_overlay:
        values_overlay_merge = (
            "\"{yq}\" eval-all 'select(fileIndex == 0) * select(fileIndex == 1)' " +
            "\"$WORK_DIR/values.yaml\" \"{overlay}\" > \"$WORK_DIR/values.yaml.tmp\"\n" +
            "mv \"$WORK_DIR/values.yaml.tmp\" \"$WORK_DIR/values.yaml\""
        ).format(
            yq = ctx.executable._yq.path,
            overlay = ctx.file.values_overlay.path,
        )
        extra_inputs.append(ctx.file.values_overlay)

    ctx.actions.run_shell(
        outputs = [output],
        inputs = extra_inputs,
        tools = [ctx.executable._helm] + ([ctx.executable._yq] if ctx.file.values_overlay else []),
        command = """\
set -euo pipefail
# Copy chart to writable directory for patching (sandbox inputs are read-only)
WORK_DIR=$(mktemp -d)
cp -rL "{chart_dir}/." "$WORK_DIR/"
# Exclude Bazel build files from the chart package
cat > "$WORK_DIR/.helmignore" << 'HELMIGNORE'
BUILD
BUILD.bazel
*.bzl
.git/
HELMIGNORE
{url_patch}
{values_overlay_merge}
"{helm}" package "$WORK_DIR" --destination "{out_dir}"
# helm package outputs <name>-<version>.tgz, find and move it
TGZ=$(ls "{out_dir}"/*.tgz)
mv "$TGZ" "{output}"
rm -rf "$WORK_DIR"
""".format(
            helm = ctx.executable._helm.path,
            chart_dir = chart_dir,
            out_dir = output.dirname,
            output = output.path,
            url_patch = url_patch,
            values_overlay_merge = values_overlay_merge,
        ),
        mnemonic = "HelmPackage",
        progress_message = "Packaging Helm chart %s" % ctx.label.name,
    )

    return [DefaultInfo(files = depset([output]))]

helm_package = rule(
    implementation = _helm_package_impl,
    attrs = {
        "srcs": attr.label_list(
            mandatory = True,
            allow_files = True,
            doc = "Chart source files (typically from glob([\"**/*\"]))",
        ),
        "url": attr.string(
            doc = "URL to inject into Chart.yaml org.opencontainers.image.url annotation. " +
                  "Used by GHCR to deep-link to the chart's source directory.",
        ),
        "values_overlay": attr.label(
            allow_single_file = True,
            doc = "Optional generated values file to deep-merge into the chart's " +
                  "values.yaml before packaging (e.g. the output of helm_images_values).",
        ),
        "_helm": attr.label(
            default = "@multitool//tools/helm",
            executable = True,
            cfg = "exec",
        ),
        "_yq": attr.label(
            default = "@multitool//tools/yq",
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Packages a Helm chart directory into a .tgz archive.",
)

# --- helm_push: run-time rule that pushes a .tgz to an OCI registry ---

def _helm_push_impl(ctx):
    """Push a packaged Helm chart to an OCI registry."""
    push_script = ctx.actions.declare_file(ctx.label.name + ".bash")

    workspace_name = ctx.workspace_name

    # Resolve chart-version.sh path if provided
    chart_version_sh_path = ""
    if ctx.file._chart_version_sh:
        chart_version_sh_path = _rlocationpath(ctx.file._chart_version_sh, workspace_name)

    ctx.actions.expand_template(
        template = ctx.file._push_template,
        output = push_script,
        is_executable = True,
        substitutions = {
            "{{HELM}}": _rlocationpath(ctx.executable._helm, workspace_name),
            "{{CRANE}}": _rlocationpath(ctx.executable._crane, workspace_name),
            "{{CHART_TGZ}}": _rlocationpath(ctx.file.chart, workspace_name),
            "{{REPOSITORY}}": ctx.attr.repository,
            "{{CHART_VERSION_SH}}": chart_version_sh_path,
            "{{CHART_DIR}}": ctx.attr.chart_dir,
        },
    )

    runfiles_files = [ctx.file.chart]
    if ctx.file._chart_version_sh:
        runfiles_files.append(ctx.file._chart_version_sh)

    runfiles = ctx.runfiles(files = runfiles_files)
    runfiles = runfiles.merge(ctx.attr._helm[DefaultInfo].default_runfiles)
    runfiles = runfiles.merge(ctx.attr._crane[DefaultInfo].default_runfiles)
    runfiles = runfiles.merge(ctx.attr._runfiles[DefaultInfo].default_runfiles)

    return [DefaultInfo(
        executable = push_script,
        runfiles = runfiles,
    )]

helm_push = rule(
    implementation = _helm_push_impl,
    attrs = {
        "chart": attr.label(
            mandatory = True,
            allow_single_file = [".tgz"],
            doc = "Packaged Helm chart (.tgz from helm_package)",
        ),
        "repository": attr.string(
            mandatory = True,
            doc = "OCI repository URL (e.g., oci://ghcr.io/user/repo/charts)",
        ),
        "chart_dir": attr.string(
            default = "",
            doc = "Source chart directory path (for auto-versioning). Empty disables versioning.",
        ),
        "_chart_version_sh": attr.label(
            default = "//bazel/helm:chart-version.sh",
            allow_single_file = True,
        ),
        "_push_template": attr.label(
            default = "//bazel/helm:push.sh.tpl",
            allow_single_file = True,
        ),
        "_helm": attr.label(
            default = "@multitool//tools/helm",
            executable = True,
            cfg = "exec",
        ),
        "_crane": attr.label(
            default = "@multitool//tools/crane",
            executable = True,
            cfg = "exec",
        ),
        "_runfiles": attr.label(
            default = "@bazel_tools//tools/bash/runfiles",
        ),
    },
    executable = True,
    doc = "Pushes a packaged Helm chart (.tgz) to an OCI registry.",
)
