"""private_oci_layer: fetch one layer blob from a private registry.

rules_oci's oci.pull makes its repository public to the whole workspace. This
rule downloads only the pinned layer, checks its sha256, and exports it to one
package. It authenticates like rules_oci: it reads the Docker config that
`docker login` writes ($DOCKER_CONFIG or ~/.docker/config.json).
"""

def _docker_auth(rctx, registry):
    config_dir = rctx.os.environ.get("DOCKER_CONFIG") or (rctx.os.environ.get("HOME", "") + "/.docker")
    path = rctx.path(config_dir + "/config.json")
    if not path.exists:
        fail("private_oci_layer: no Docker config at %s; run `docker login %s`" % (path, registry))
    auths = json.decode(rctx.read(path)).get("auths", {})
    entry = auths.get(registry) or auths.get("https://" + registry)
    if not entry or not entry.get("auth"):
        fail("private_oci_layer: %s has no inline auth for %s" % (path, registry))
    return entry["auth"]

def _private_oci_layer_impl(rctx):
    registry = rctx.attr.registry
    basic = _docker_auth(rctx, registry)
    token_url = "https://%s/token?scope=repository:%s:pull&service=%s" % (registry, rctx.attr.repository, registry)
    rctx.download(
        url = token_url,
        output = "token.json",
        auth = {token_url: {"type": "pattern", "pattern": "Basic <password>", "password": basic}},
    )
    token = json.decode(rctx.read("token.json"))["token"]
    rctx.delete("token.json")
    blob_url = "https://%s/v2/%s/blobs/sha256:%s" % (registry, rctx.attr.repository, rctx.attr.sha256)
    rctx.download(
        url = blob_url,
        output = "layer.tar.gz",
        sha256 = rctx.attr.sha256,
        auth = {blob_url: {"type": "pattern", "pattern": "Bearer <password>", "password": token}},
    )

    # In this repository "//pkg" names its own packages; "@@//pkg" is the main one.
    visibility = ["@@" + v if v.startswith("//") else v for v in rctx.attr.visibility_to]
    rctx.file("BUILD.bazel", 'exports_files(["layer.tar.gz"], visibility = %s)\n' % repr(visibility))

private_oci_layer = repository_rule(
    implementation = _private_oci_layer_impl,
    attrs = {
        "registry": attr.string(mandatory = True),
        "repository": attr.string(mandatory = True),
        "sha256": attr.string(mandatory = True, doc = "The layer blob digest, without the sha256: prefix."),
        "visibility_to": attr.string_list(mandatory = True),
    },
    environ = ["DOCKER_CONFIG", "HOME"],
)
