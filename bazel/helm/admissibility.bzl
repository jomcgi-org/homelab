"""Render targets for first-party chart admissibility validation."""


def helm_admissibility_render(
        name,
        chart,
        chart_files,
        release_name,
        namespace,
        values_files = [],
        target_suffix = "",
        visibility = ["//visibility:public"]):
    """Renders a first-party chart with the values used by its Application."""
    cmd = [
        "$(location @multitool//tools/helm)",
        "template",
        release_name,
        chart,
        "--namespace",
        namespace,
        "--include-crds",
    ]

    for values_file in values_files:
        cmd.extend(["--values", "$(location {})".format(values_file)])

    native.genrule(
        name = name,
        srcs = [chart_files] + values_files,
        outs = ["manifests" + target_suffix + "/all.yaml"],
        cmd = " ".join(cmd) + " > $@",
        tools = ["@multitool//tools/helm"],
        local = True,
        tags = ["manual"],
        visibility = visibility,
    )
