"""Rule that writes one line per pushable image: push label, repository, digest.

Exists so main's deploy can decide WHICH images to push without staging any of
them. `bazel run` materialises every command's runfiles on the CI runner before
the first command executes, so running the full push_all multirun drags all ~24
dual-arch images out of CAS even when every action is a cache hit. That is the
single largest source of BuildBuddy download traffic in this repo (measured
2026-08-10: the `deploy` runner alone was 488 GB/day, 29.6% of the total).

The manifest is the cheap half of that data: three short strings per image,
built from the same OciImageInfo the Helm values are pinned from, so the digest
here is exactly the digest a chart would deploy. bazel/images/push/push-changed.sh
reads it, asks the registry whether each digest is already published, and runs
only the pushes that would actually change something.

Digests are content-stable across rebuilds: --stamp feeds only oci_push's
remote_tags, never the image itself. The missed-chart-bump guard
(bazel/helm/check-missed-bump.sh) already depends on that same property.

NAMING CONSTRAINT: this rule must not contain `oci_push`, `apko_push` or
`helm_push` as a substring. `kind()` is a REGEX and
validate-generate-scripts.sh's queries are unanchored, so an earlier
`oci_push_manifest` was matched by `kind("oci_push", //...)` and then reported
as missing from bazel/images/BUILD, which the grep generator never emits it
into. The collision is invisible locally: it only shows up in pr-checks, since
`ci test` does not run that validator.
"""

load("//bazel/tools/oci:providers.bzl", "OciImageInfo")

def _oci_digest_manifest_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".txt")

    inputs = []
    commands = ["set -euo pipefail", "> " + output.path]

    # Sorted so the manifest is byte-stable regardless of dict iteration order,
    # which keeps this action cacheable across runs that changed nothing.
    entries = sorted(
        [(push_label, target) for target, push_label in ctx.attr.images.items()],
        key = lambda pair: pair[0],
    )

    for push_label, target in entries:
        info = target[OciImageInfo]
        inputs.append(info.repository)
        inputs.append(info.digest)

        # Both files are read with $(cat), which strips the trailing newline the
        # rules_oci digest file carries and the repository file does not.
        commands.append(
            'printf "%s\\t%s\\t%s\\n" "{label}" "$(cat {repository})" "$(cat {digest})" >> {output}'.format(
                label = push_label,
                repository = info.repository.path,
                digest = info.digest.path,
                output = output.path,
            ),
        )

    ctx.actions.run_shell(
        outputs = [output],
        inputs = depset(inputs),
        command = "\n".join(commands),
        mnemonic = "OciDigestManifest",
        progress_message = "Writing image push manifest %s" % ctx.label,
    )

    return [DefaultInfo(files = depset([output]))]

oci_digest_manifest = rule(
    implementation = _oci_digest_manifest_impl,
    attrs = {
        "images": attr.label_keyed_string_dict(
            mandatory = True,
            providers = [OciImageInfo],
            doc = "Maps each image's `.info` target to the label of its `.push` " +
                  "target. Requiring OciImageInfo is deliberate: a new image " +
                  "with no `.info` sibling fails at analysis rather than " +
                  "silently dropping out of the manifest, which would make " +
                  "push-changed.sh skip an image that genuinely changed.",
        ),
    },
    doc = "Writes a `push_label<TAB>repository<TAB>digest` manifest for a set of images.",
)
