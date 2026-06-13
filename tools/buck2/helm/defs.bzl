"""Minimal Helm chart packaging — wraps the pinned `helm`.

`helm_chart` stages a chart (files under a `chart/` dir) and runs `helm package`
to produce a versioned `.tgz`. `deps` (e.g. an oci_image) are built but not
packaged, so the chart target *depends on* the image it deploys — building the
chart builds the image. This is the Buck2 counterpart to bazel/helm's helm_chart
(digest-pinned image-value injection can be layered on later via helm values).
"""

_HELM = "//tools/buck2/bin:helm"

def helm_chart(name, srcs, deps = [], visibility = ["PUBLIC"], **kwargs):
    """Package a Helm chart into a `.tgz`.

    Args:
      name: target name; output is the packaged chart `.tgz`.
      srcs: chart files under a `chart/` dir (e.g. glob(["chart/**"])).
      deps: targets the chart depends on (e.g. an oci_image) — built, not packaged.
      visibility: target visibility.
    """
    native.genrule(
        name = name,
        srcs = srcs + deps,
        out = "chart.tgz",
        # Stage only the files under `.../chart/` (preserving structure), skipping
        # any deps, then `helm package`. Shell $-vars ($f/$rel/$d) are expanded by
        # sh; buck2 only substitutes $SRCS/$TMP/$OUT and $(...) macros.
        cmd = " && ".join([
            "mkdir -p \"$TMP/chart\"",
            "for f in $SRCS; do " +
            "case \"$f\" in */chart/*) ;; *) continue ;; esac; " +
            "rel=\"${f##*/chart/}\"; " +
            "case \"$rel\" in */*) d=\"${rel%/*}\" ;; *) d=\".\" ;; esac; " +
            "mkdir -p \"$TMP/chart/$d\"; cp \"$f\" \"$TMP/chart/$rel\"; " +
            "done",
            "$(exe {helm}) package \"$TMP/chart\" -d \"$TMP/out\" >/dev/null".format(helm = _HELM),
            "mv \"$TMP/out\"/*.tgz $OUT",
        ]),
        visibility = visibility,
        **kwargs
    )
