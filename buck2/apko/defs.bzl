"""Wolfi/apko OCI image rules — wrap the pinned `apko` CLI.

`apko_image` builds a (multi-arch) OCI image tarball from an apko config + a
committed lockfile. apko is a Go binary that assembles OCI images from apk
packages with no Dockerfile/daemon, builds a multi-arch index natively, and is
reproducible — so a single `genrule` covers what rules_oci needs several rules +
platform transitions to do. The lockfile (`apko lock`) pins exact package
versions for reproducibility; commit it alongside the config and refresh with
`apko lock` when the config changes.

The output `<name>.tar` is a standard OCI image (an index when the config lists
>1 arch) and is consumable by `crane push` / `crane index`. Pushing + runtime
tags live in a separate step (see apko_push / the oci push rules).
"""

_APKO = "//buck2/bin:apko"

def apko_image(name, config, lock, visibility = ["PUBLIC"], **kwargs):
    """Build an OCI image tar from an apko `config` + `lock`.

    Args:
      name: target name; output is `<name>`'s `image.tar`.
      config: apko YAML config (its `archs:` list drives multi-arch).
      lock: `apko.lock.json` produced by `apko lock` (pins package versions).
      visibility: target visibility.
    """

    # apko fetches packages over the network during build; the lockfile pins
    # versions so the result is reproducible. SBOM generation is disabled here
    # (it would write undeclared sidecar files next to $OUT); add a dedicated
    # SBOM target later if needed. --cache-dir is kept inside the action's $TMP.
    #
    # Source files are referenced via $SRCS positionally ($1=config, $2=lock) —
    # buck2 has no implicit source-file targets, so $(location) can't name them.
    native.genrule(
        name = name,
        srcs = [config, lock],
        out = "image.tar",
        cmd = " && ".join([
            "mkdir -p \"$TMP/cache\"",
            "set -- $SRCS",
            "$(exe {apko}) build --lockfile \"$2\" --cache-dir \"$TMP/cache\" --sbom=false \"$1\" {name}:latest \"$OUT\"".format(
                apko = _APKO,
                name = name,
            ),
        ]),
        # apko fetches apk packages over the network — opt this genrule out of
        # remote execution (RE sandboxes network) so it runs on the local runner.
        labels = ["uses_undeclared_inputs"],
        visibility = visibility,
        **kwargs
    )
