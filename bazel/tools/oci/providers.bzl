"OciImageInfo provider and oci_image_info rule for exposing image metadata to helm_chart."

OciImageInfo = provider(
    doc = "Provides OCI image repository, tag, and digest information for use by helm_chart.",
    fields = {
        "repository": "File containing the OCI repository URL (plain text, no trailing newline)",
        "image_tags": "File containing the image tags (one per line; first line is primary tag)",
        "digest": "File containing the content-addressed manifest/index digest (sha256:...), from the image's rules_oci :{name}.digest target. Pinned into Helm values so the deployed reference is content-stable (a chart republish rolls a container only when its digest actually changes).",
    },
)

def _oci_image_info_impl(ctx):
    """Write the repository URL to a file and expose OciImageInfo."""
    repo_file = ctx.actions.declare_file(ctx.label.name + ".repository")
    ctx.actions.write(repo_file, ctx.attr.repository)

    # ctx.attr.image is intentionally not read here: it exists only so the image
    # (and its bundled tars) sits in this target's dependency graph. That widens
    # the dependency-query closure the chart-version bot walks
    # (`deps(chart.package)`), so a change to content layered into the image, e.g.
    # fc-agent-init bundled into the harness image, correctly bumps the dependent
    # chart. It is deliberately NOT added to DefaultInfo, so the image is never
    # packaged into the chart itself.
    return [
        DefaultInfo(files = depset([repo_file])),
        OciImageInfo(
            repository = repo_file,
            image_tags = ctx.file.image_tags,
            digest = ctx.file.image_digest,
        ),
    ]

oci_image_info = rule(
    implementation = _oci_image_info_impl,
    attrs = {
        "repository": attr.string(
            mandatory = True,
            doc = "OCI repository URL (e.g. ghcr.io/jomcgi/homelab/my-app)",
        ),
        "image_tags": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "Tags file produced by the CI stamp step (first line is the primary tag)",
        ),
        "image_digest": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "The image's rules_oci :{name}.digest file (a raw sha256:... line). " +
                  "Pinned into Helm values as the content-stable deployed reference.",
        ),
        "image": attr.label(
            doc = "The image target this info describes. Only widens the dependency " +
                  "closure for the chart-version bot; not built into outputs.",
        ),
    },
    doc = "Exposes OCI image repository, tag, and digest information via OciImageInfo provider.",
)
