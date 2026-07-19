"""Rule to generate a Helm values YAML fragment from OciImageInfo providers."""

load("//bazel/tools/oci:providers.bzl", "OciImageInfo")

def _helm_images_values_impl(ctx):
    """Generate a Helm values YAML fragment mapping image keys to repository + tag.

    For each entry in ctx.attr.images (label → yaml_path_string), reads the
    OciImageInfo provider from the label and emits:

        <yaml_path>:
          repository: <repo>
          tag: <primary_tag>

    Dot-notation in yaml_path is expanded to nested YAML keys, so
    "sidecar.image" becomes:

        sidecar:
          image:
            repository: ...
            tag: ...
    """
    output = ctx.actions.declare_file(ctx.label.name + ".yaml")

    all_inputs = []

    # Build the shell command incrementally for each image entry.
    # We initialise the output file to empty, then append one block per image.
    cmd_lines = [
        "set -euo pipefail",
        # Initialise (truncate) the output file
        "> " + output.path,
    ]

    for target, yaml_path in ctx.attr.images.items():
        info = target[OciImageInfo]
        all_inputs.append(info.repository)
        all_inputs.append(info.image_tags)
        all_inputs.append(info.digest)

        # Expand dot-notation into nested YAML keys.
        # e.g. "image" → ["image"]
        #      "sidecar.image" → ["sidecar", "image"]
        parts = yaml_path.split(".")

        # Emit parent keys with increasing indentation.
        # Single-quoted echo is safe for YAML key names (alphanumeric + dots).
        for depth, part in enumerate(parts):
            indent = "  " * depth
            cmd_lines.append(
                "echo '{indent}{part}:' >> {out}".format(
                    indent = indent,
                    part = part,
                    out = output.path,
                ),
            )

        # The leaf indent is one level deeper than the last key
        leaf_indent = "  " * len(parts)

        # Read repo and primary tag from files at execution time.
        # The concatenation of a single-quoted literal and a double-quoted
        # command substitution is standard POSIX shell.
        cmd_lines.append(
            "echo '{leaf_indent}repository: '\"$(cat {repo})\" >> {out}".format(
                leaf_indent = leaf_indent,
                repo = info.repository.path,
                out = output.path,
            ),
        )
        cmd_lines.append(
            "echo '{leaf_indent}tag: '\"$(head -1 {tags})\" >> {out}".format(
                leaf_indent = leaf_indent,
                tags = info.image_tags.path,
                out = output.path,
            ),
        )

        # Content-addressed digest (sha256:...) from the image's rules_oci
        # :{name}.digest target. Charts reference the image as
        # `repository@digest` so an unchanged image renders an identical
        # Deployment (no spurious rollout on a chart republish); only a real
        # content change moves the digest. `tag` above is retained for the
        # missed-bump guard and any label use, but is not the deployed ref.
        cmd_lines.append(
            "echo '{leaf_indent}digest: '\"$(head -1 {digest})\" >> {out}".format(
                leaf_indent = leaf_indent,
                digest = info.digest.path,
                out = output.path,
            ),
        )

    ctx.actions.run_shell(
        outputs = [output],
        inputs = all_inputs,
        command = "\n".join(cmd_lines),
        mnemonic = "HelmImagesValues",
        progress_message = "Generating Helm image values for %s" % ctx.label.name,
    )

    return [DefaultInfo(files = depset([output]))]

helm_images_values = rule(
    implementation = _helm_images_values_impl,
    attrs = {
        # Keys are image labels (carrying OciImageInfo); values are YAML path strings.
        # Using label_keyed_string_dict because Bazel resolves dict keys as labels,
        # allowing provider access.  The helm_chart macro inverts the user-supplied
        # {yaml_path: label} dict before calling this rule.
        "images": attr.label_keyed_string_dict(
            mandatory = True,
            allow_files = False,
            providers = [OciImageInfo],
            doc = "Map of image labels → YAML path strings (dot-notation supported).",
        ),
    },
    doc = "Generates a Helm values YAML fragment from OciImageInfo providers.",
)
