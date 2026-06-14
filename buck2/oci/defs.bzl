"""Minimal rules_oci-style image composition — wraps the pinned `regctl`.

`oci_image` layers one or more filesystem tarballs onto a base OCI image (e.g.
from `apko_image`) via regctl `image mod --layer-add`, preserving the base's
config (entrypoint, user, env). This is the Buck2 counterpart to rules_oci's
`oci_image(base=..., tars=[...])`. regctl operates on a local OCI layout
(`ocidir://`) so no registry is needed — crane append, by contrast, only takes a
registry reference as its base. Image config (entrypoint etc.) is set on the apko
base config so no separate mutate step is needed for the common case.
"""

load(":providers.bzl", "OciImageInfo")

_REGCTL = "//buck2/bin:regctl"
_CRANE = "//buck2/bin:crane"

def _oci_push_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[DefaultInfo].default_outputs[0]
    sh = ctx.actions.write(
        "oci_push.sh",
        "\n".join([
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "CRANE=\"$1\"; IMAGE=\"$2\"; REPO=\"$3\"; shift 3",
            "if [ \"$#\" -eq 0 ]; then echo \"usage: buck2 run <target> -- <tag>...\" >&2; exit 2; fi",
            "for t in \"$@\"; do echo \"pushing $REPO:$t\" >&2; \"$CRANE\" push \"$IMAGE\" \"$REPO:$t\"; done",
        ]),
        is_executable = True,
    )
    return [
        DefaultInfo(),
        RunInfo(args = cmd_args([sh, ctx.attrs._crane[RunInfo], image, ctx.attrs.repository])),
    ]

# Runnable: `buck2 run <image>.push -- <tag>...` pushes the image tar to its
# repository at each runtime-supplied tag (via crane). Tags are runtime args (the
# Buck2 runtime-arg analogue of bazel's stamped remote_tags); see image_tags.sh.
oci_push = rule(
    impl = _oci_push_impl,
    attrs = {
        "image": attrs.dep(),
        "repository": attrs.string(),
        "_crane": attrs.exec_dep(default = _CRANE, providers = [RunInfo]),
    },
)

def _oci_image_info_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[DefaultInfo].default_outputs[0]
    digest = ctx.actions.declare_output("digest")

    # The image tar is an OCI layout; the first sha256 in index.json is the
    # (single-arch) manifest digest — i.e. the ref you'd pull as repo@sha256:...
    ctx.actions.run(
        cmd_args([
            "bash",
            "-c",
            'set -e; D="$(mktemp -d)"; tar -xf "$1" -C "$D" index.json; grep -oE "sha256:[a-f0-9]{64}" "$D/index.json" | head -1 | tr -d "\\n" > "$2"',
            "_",  # $0
            image,
            digest.as_output(),
        ]),
        category = "oci_digest",
    )

    return [
        DefaultInfo(default_output = digest),
        OciImageInfo(repository = ctx.attrs.repository, digest = digest),
    ]

# Exposes OciImageInfo (repository + content digest) for an image tar, so
# helm_images_values can digest-pin the chart. Mirrors bazel's oci_image_info.
oci_image_info = rule(
    impl = _oci_image_info_impl,
    attrs = {
        "image": attrs.dep(),
        "repository": attrs.string(),
    },
)

def oci_image(name, base, layers = [], platform = "linux/amd64", repository = None, visibility = ["PUBLIC"], **kwargs):
    """Layer `layers` (filesystem tarballs) onto `base` (an OCI image tar).

    Args:
      name: target name; output is `<name>`'s `image.tar` (an OCI image tar).
      base: an OCI image tar target (e.g. an apko_image).
      layers: list of tar targets, each a filesystem layer to add (lowest first).
      platform: platform of the layers (matches the single-arch base).
      visibility: target visibility.
    """
    add_args = " ".join([
        "--layer-add \"tar=$(location {layer}),platform={p}\"".format(layer = layer, p = platform)
        for layer in layers
    ])
    native.genrule(
        name = name,
        out = "image.tar",
        # Import the base tar into a local OCI layout, add the layers, export the
        # result. All in the action's $TMP; regctl runs offline (RE-eligible).
        cmd = " && ".join([
            "rm -rf \"$TMP/oci\"",
            "$(exe {rc}) image import ocidir://$TMP/oci:base $(location {base})".format(rc = _REGCTL, base = base),
            "$(exe {rc}) image mod ocidir://$TMP/oci:base --create ocidir://$TMP/oci:img {add}".format(rc = _REGCTL, add = add_args),
            "$(exe {rc}) image export ocidir://$TMP/oci:img $OUT".format(rc = _REGCTL),
        ]),
        visibility = visibility,
        **kwargs
    )

    # Expose OciImageInfo (repository + digest) for helm digest-pinning, like
    # bazel's go_image/apko_image `.info` sub-target, and a `.push` runnable.
    if repository:
        oci_image_info(
            name = name + ".info",
            image = ":" + name,
            repository = repository,
            visibility = visibility,
        )
        oci_push(
            name = name + ".push",
            image = ":" + name,
            repository = repository,
            visibility = visibility,
        )

def tar_layer(name, binary, path, visibility = ["PUBLIC"], **kwargs):
    """Package a single executable `binary` target into a layer tar at `path`.

    Args:
      name: target name; output is `<name>`'s `layer.tar`.
      binary: an executable target (e.g. a rust_binary) — its output is staged at `path`.
      path: absolute in-image path for the binary (e.g. "/usr/local/bin/server").
      visibility: target visibility.
    """

    # Compute the parent dir in Starlark — buck2 would parse a shell $(dirname ...)
    # as a (failing) target macro.
    parent = path.rsplit("/", 1)[0]
    native.genrule(
        name = name,
        out = "layer.tar",
        # Stage the binary at `path` inside a root dir, mark it executable, tar it.
        cmd = " && ".join([
            "mkdir -p \"$TMP/root{parent}\"".format(parent = parent),
            "cp $(location {bin}) \"$TMP/root{p}\"".format(bin = binary, p = path),
            "chmod +x \"$TMP/root{p}\"".format(p = path),
            "tar -C \"$TMP/root\" -cf $OUT .",
        ]),
        visibility = visibility,
        **kwargs
    )
