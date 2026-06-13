"""Minimal rules_oci-style image composition — wraps the pinned `regctl`.

`oci_image` layers one or more filesystem tarballs onto a base OCI image (e.g.
from `apko_image`) via regctl `image mod --layer-add`, preserving the base's
config (entrypoint, user, env). This is the Buck2 counterpart to rules_oci's
`oci_image(base=..., tars=[...])`. regctl operates on a local OCI layout
(`ocidir://`) so no registry is needed — crane append, by contrast, only takes a
registry reference as its base. Image config (entrypoint etc.) is set on the apko
base config so no separate mutate step is needed for the common case.
"""

_REGCTL = "//tools/buck2/bin:regctl"

def oci_image(name, base, layers = [], platform = "linux/amd64", visibility = ["PUBLIC"], **kwargs):
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
