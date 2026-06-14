"""Pinned host CLI binaries for the buck2 image/helm rules.

`pinned_cli` emits, for a tool, one `http_file` per host platform, a `genrule`
that extracts the binary, and an OS+CPU-selected `command_alias`. These tools run
on the *build host* (darwin/arm64 locally, linux in CI), so the alias selects on
both OS and CPU. darwin is wired arm64-only.

Mirrors loom's tools/BUCK pattern (http_file + extract genrule + command_alias).
"""

# Canonical host labels every tool is wired for.
_LABELS = ["darwin-arm64", "linux-amd64", "linux-arm64"]

def pinned_cli(name, binary, urls, shas, archive, visibility = ["PUBLIC"]):
    """Define a host-selected pinned CLI.

    Args:
      name: alias target name (and the `buck2 run //buck2/bin:<name>` handle).
      binary: the executable's filename inside the archive (== alias output name).
      urls: {label: download_url} for each of _LABELS.
      shas: {label: sha256} for each of _LABELS.
      archive: "raw" (url is the bare binary), "tar_root" (binary at tar root),
               or "tar_strip1" (binary one dir deep, e.g. helm's <os>-<arch>/helm).
      visibility: alias visibility.
    """
    raw = archive == "raw"
    suffix = ".bin" if raw else ".tar.gz"
    for label in _LABELS:
        native.http_file(
            name = "{}-{}{}".format(name, label, suffix),
            urls = [urls[label]],
            sha256 = shas[label],
        )
        src = ":{}-{}{}".format(name, label, suffix)
        if raw:
            cmd = "cp $(location {src}) $OUT && chmod +x $OUT".format(src = src)
        else:
            strip = " --strip-components=1" if archive == "tar_strip1" else ""
            cmd = "mkdir -p $TMP/x && tar -xzf $(location {src}) -C $TMP/x{strip} && cp $TMP/x/{bin} $OUT && chmod +x $OUT".format(
                src = src,
                strip = strip,
                bin = binary,
            )
        native.genrule(
            name = "{}-{}".format(name, label),
            out = binary,
            cmd = cmd,
            executable = True,
        )
    native.command_alias(
        name = name,
        exe = select({
            "prelude//os/constraints:macos": ":{}-darwin-arm64".format(name),
            "prelude//os/constraints:linux": select({
                "prelude//cpu/constraints:x86_64": ":{}-linux-amd64".format(name),
                "prelude//cpu/constraints:arm64": ":{}-linux-arm64".format(name),
            }),
        }),
        visibility = visibility,
    )
