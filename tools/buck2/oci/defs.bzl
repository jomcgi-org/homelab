"""Minimal rules_oci-style image composition — wraps the pinned `crane`.

`oci_image` layers one or more filesystem tarballs onto a base OCI image (e.g.
from `apko_image`) via `crane append`, preserving the base's config (entrypoint,
user, env). This is the Buck2 counterpart to rules_oci's `oci_image(base=...,
tars=[...])`. Image config (entrypoint etc.) is set on the apko base config so no
separate mutate step is needed for the common case.
"""

_CRANE = "//tools/buck2/bin:crane"

def oci_image(name, base, layers = [], visibility = ["PUBLIC"], **kwargs):
    """Append `layers` (filesystem tarballs) onto `base` (an OCI image tar).

    Args:
      name: target name; output is `<name>`'s `image.tar` (an OCI image tar).
      base: an OCI image tar target (e.g. an apko_image).
      layers: list of tar targets, each a filesystem layer to add (lowest first).
      visibility: target visibility.
    """
    f_args = " ".join(["-f $(location {})".format(layer) for layer in layers])
    native.genrule(
        name = name,
        out = "image.tar",
        # crane append is offline (operates on local tarballs); base + layers are
        # declared inputs via $(location), so this can run on RE.
        cmd = "$(exe {crane}) append -b $(location {base}) {fargs} -o $OUT".format(
            crane = _CRANE,
            base = base,
            fargs = f_args,
        ),
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
