"""Extract the Envoy executable from a rules_oci image layout."""

_BUILD_FILE = """\
exports_files(
    ["envoy"],
    visibility = ["//visibility:public"],
)
"""

def _blob_path(layout, digest):
    algorithm, value = digest.split(":", 1)
    return str(layout) + "/blobs/" + algorithm + "/" + value

def _oci_envoy_impl(rctx):
    layout = rctx.path(rctx.attr.image).dirname
    index = json.decode(rctx.read(rctx.path(str(layout) + "/index.json")))
    manifests = index.get("manifests", [])
    if not manifests:
        fail("Envoy OCI index contains no manifest")

    manifest_path = _blob_path(layout, manifests[0]["digest"])
    manifest = json.decode(rctx.read(rctx.path(manifest_path)))
    extracted = False
    for layer in manifest.get("layers", []):
        layer_path = _blob_path(layout, layer["digest"])
        contains = rctx.execute(
            ["tar", "tzf", layer_path, "usr/local/bin/envoy"],
            timeout = 120,
        )
        if contains.return_code != 0:
            continue
        result = rctx.execute(
            ["tar", "xzf", layer_path, "usr/local/bin/envoy"],
            timeout = 120,
        )
        if result.return_code != 0:
            fail("Failed to extract Envoy: " + result.stderr)
        result = rctx.execute(
            ["mv", "usr/local/bin/envoy", "envoy"],
            timeout = 30,
        )
        if result.return_code != 0:
            fail("Failed to stage Envoy: " + result.stderr)
        extracted = True

    if not extracted:
        fail("usr/local/bin/envoy was not found in the OCI image")

    rctx.file("BUILD.bazel", _BUILD_FILE)

oci_envoy = repository_rule(
    implementation = _oci_envoy_impl,
    attrs = {
        "image": attr.label(
            mandatory = True,
            allow_single_file = True,
        ),
    },
)
