"""Minimal Helm chart packaging — wraps the pinned `helm`/`yq`.

`helm_chart` stages a chart (files under a `chart/` dir) and runs `helm package`
to produce a versioned `.tgz`. `deps` (e.g. an oci_image) are built but not
packaged, so the chart target *depends on* the image it deploys. `images=` pins
chart image references by digest (via `helm_images_values`, the Buck2 counterpart
to bazel/helm's `helm_chart(images=...)` + `helm_images_values`). `lint=True`
adds a `helm lint` validation target.
"""

load("//tools/buck2/oci:providers.bzl", "OciImageInfo")

_HELM = "//tools/buck2/bin:helm"
_YQ = "//tools/buck2/bin:yq"
_KUBECONFORM = "//tools/buck2/bin:kubeconform"

def _helm_images_values_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("images.yaml")

    # Args: yq, out, then a (yaml-path, repository, digest-file) triple per image.
    triples = []
    for path, dep in ctx.attrs.images.items():
        info = dep[OciImageInfo]
        triples += [path, info.repository, info.digest]

    # Build values like `<path>.repository: <repo>` / `<path>.digest: sha256:...`
    # for each image (dot-paths become nested keys), digest read from its file.
    script = (
        'set -e; YQ="$1"; OUT="$2"; shift 2; echo "{}" > "$OUT"; ' +
        'while [ "$#" -ge 3 ]; do P="$1"; R="$2"; DF="$3"; shift 3; D="$(cat "$DF")"; ' +
        '"$YQ" -i ".${P}.repository = \\"$R\\" | .${P}.digest = \\"$D\\"" "$OUT"; done'
    )
    ctx.actions.run(
        cmd_args(["bash", "-c", script, "_", ctx.attrs._yq[RunInfo], out.as_output()] + triples),
        category = "helm_images_values",
    )
    return [DefaultInfo(default_output = out)]

# Generates a values overlay pinning each image's repository + digest from its
# OciImageInfo. Mirrors bazel/helm's helm_images_values.
helm_images_values = rule(
    impl = _helm_images_values_impl,
    attrs = {
        "images": attrs.dict(attrs.string(), attrs.dep(providers = [OciImageInfo])),
        "_yq": attrs.exec_dep(default = _YQ, providers = [RunInfo]),
    },
)

def _helm_push_impl(ctx: AnalysisContext) -> list[Provider]:
    chart = ctx.attrs.chart[DefaultInfo].default_outputs[0]
    sh = ctx.actions.write(
        "helm_push.sh",
        "\n".join([
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "HELM=\"$1\"; CHART=\"$2\"; REPO=\"$3\"",
            "echo \"pushing $CHART -> $REPO\" >&2",
            "\"$HELM\" push \"$CHART\" \"$REPO\"",
        ]),
        is_executable = True,
    )
    return [
        DefaultInfo(),
        RunInfo(args = cmd_args([sh, ctx.attrs._helm[RunInfo], chart, ctx.attrs.repository])),
    ]

# Runnable: `buck2 run <chart>.push` pushes the packaged chart .tgz to an OCI
# registry (helm push). Mirrors bazel/helm's helm_push.
helm_push = rule(
    impl = _helm_push_impl,
    attrs = {
        "chart": attrs.dep(),
        "repository": attrs.string(),
        "_helm": attrs.exec_dep(default = _HELM, providers = [RunInfo]),
    },
)

_ARGOCD_APP_TEMPLATE = """apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
  namespace: argocd
spec:
  source:
    repoURL: {repo}
    chart: {chart}
    targetRevision: {rev}
    helm:
      releaseName: {release}
  destination:
    server: {server}
    namespace: {ns}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
"""

def _argocd_app_impl(ctx: AnalysisContext) -> list[Provider]:
    content = _ARGOCD_APP_TEMPLATE.format(
        name = ctx.attrs.app_name or ctx.label.name,
        repo = ctx.attrs.chart_repo,
        chart = ctx.attrs.chart,
        rev = ctx.attrs.target_revision,
        release = ctx.attrs.release_name or ctx.attrs.app_name or ctx.label.name,
        server = ctx.attrs.dest_server,
        ns = ctx.attrs.namespace,
    )
    out = ctx.actions.write("application.yaml", content)
    return [DefaultInfo(default_output = out)]

# Generates an ArgoCD Application that deploys the chart published to an OCI repo
# (chart + targetRevision), mirroring bazel/helm's argocd_app / deploy manifests.
argocd_app = rule(
    impl = _argocd_app_impl,
    attrs = {
        "chart": attrs.string(),  # chart name in the OCI repo
        "target_revision": attrs.string(),  # chart version
        "namespace": attrs.string(default = "default"),
        "release_name": attrs.string(default = ""),
        "app_name": attrs.string(default = ""),  # Application metadata.name (defaults to target name)
        "chart_repo": attrs.string(default = "ghcr.io/jomcgi/homelab/charts"),
        "dest_server": attrs.string(default = "https://kubernetes.default.svc"),
    },
)

def helm_chart(name, srcs, deps = [], images = None, lint = True, publish = False, repository = "oci://ghcr.io/jomcgi/homelab/charts", visibility = ["PUBLIC"], **kwargs):
    """Package a Helm chart into a `.tgz`.

    Args:
      name: target name; output is the packaged chart `.tgz`.
      srcs: chart files under a `chart/` dir (e.g. glob(["chart/**"])).
      deps: targets the chart depends on (e.g. an oci_image) — built, not packaged.
      images: {yaml-path: image `.info` label} — digest-pinned into values.yaml.
      lint: also create a `<name>.lint` target running `helm lint`.
      publish: also create a `<name>.push` runnable (`helm push` to `repository`).
      repository: OCI repository to publish the chart to.
      visibility: target visibility.
    """
    overlay_srcs = []
    merge = ":"
    if images:
        helm_images_values(
            name = name + ".images_values",
            images = images,
            visibility = visibility,
        )
        overlay_srcs = [":" + name + ".images_values"]

        # Deep-merge the generated overlay into the staged chart's values.yaml.
        merge = " && ".join([
            "OV=\"\"; for f in $SRCS; do case \"$f\" in *images.yaml) OV=\"$f\" ;; esac; done",
            "if [ -n \"$OV\" ]; then $(exe {yq}) eval-all -i 'select(fi==0) * select(fi==1)' \"$TMP/chart/values.yaml\" \"$OV\"; fi".format(yq = _YQ),
        ])

    native.genrule(
        name = name,
        srcs = srcs + overlay_srcs + deps,
        out = "chart.tgz",
        cmd = " && ".join([
            "mkdir -p \"$TMP/chart\"",
            "for f in $SRCS; do " +
            "case \"$f\" in */chart/*) ;; *) continue ;; esac; " +
            "rel=\"${f##*/chart/}\"; " +
            "case \"$rel\" in */*) d=\"${rel%/*}\" ;; *) d=\".\" ;; esac; " +
            "mkdir -p \"$TMP/chart/$d\"; cp \"$f\" \"$TMP/chart/$rel\"; " +
            "done",
            merge,
            "$(exe {helm}) package \"$TMP/chart\" -d \"$TMP/out\" >/dev/null".format(helm = _HELM),
            "mv \"$TMP/out\"/*.tgz $OUT",
        ]),
        visibility = visibility,
        **kwargs
    )

    if publish:
        helm_push(
            name = name + ".push",
            chart = ":" + name,
            repository = repository,
            visibility = visibility,
        )

    if lint:
        native.genrule(
            name = name + ".lint",
            srcs = srcs,
            out = "lint.ok",
            cmd = " && ".join([
                "mkdir -p \"$TMP/chart\"",
                "for f in $SRCS; do " +
                "rel=\"${f##*/chart/}\"; " +
                "case \"$rel\" in */*) d=\"${rel%/*}\" ;; *) d=\".\" ;; esac; " +
                "mkdir -p \"$TMP/chart/$d\"; cp \"$f\" \"$TMP/chart/$rel\"; " +
                "done",
                "$(exe {helm}) lint \"$TMP/chart\" >/dev/null".format(helm = _HELM),
                "touch $OUT",
            ]),
            visibility = visibility,
        )

        # Full validation: render with `helm template`, then check the manifests
        # against Kubernetes API schemas with kubeconform (lint alone doesn't do
        # schema validation). kubeconform fetches schemas over the network, so it
        # runs locally (off RE) like apko.
        native.genrule(
            name = name + ".validate",
            srcs = srcs,
            out = "validate.ok",
            cmd = " && ".join([
                "mkdir -p \"$TMP/chart\"",
                "for f in $SRCS; do " +
                "rel=\"${f##*/chart/}\"; " +
                "case \"$rel\" in */*) d=\"${rel%/*}\" ;; *) d=\".\" ;; esac; " +
                "mkdir -p \"$TMP/chart/$d\"; cp \"$f\" \"$TMP/chart/$rel\"; " +
                "done",
                "$(exe {helm}) template validate \"$TMP/chart\" > \"$TMP/manifests.yaml\"".format(helm = _HELM),
                "$(exe {kcf}) -strict -summary \"$TMP/manifests.yaml\"".format(kcf = _KUBECONFORM),
                "touch $OUT",
            ]),
            labels = ["uses_undeclared_inputs"],
            visibility = visibility,
        )
