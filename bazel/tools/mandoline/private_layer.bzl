"""private_oci_layer: fetch one layer of a pinned private image.

rules_oci's oci.pull makes its repository public to the whole workspace. This
rule fetches the image manifest by digest, checks that it lists the pinned
layer, downloads only that layer, checks both sha256 values, and exports the
layer to one package.

It authenticates like rules_oci, from the Docker config that `docker login`
writes ($DOCKER_CONFIG or ~/.docker/config.json). Every request runs with curl
on the machine that runs Bazel, never through repository_ctx.download, so a
remote downloader never sees the credential or the registry token.
"""

_ACCEPT = "Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"

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

_HEADER = "auth.header"
_SECRETS = [_HEADER, "token.json"]

def _fail(rctx, message):
    # Starlark has no finally: remove every credential copy before failing.
    for path in _SECRETS:
        rctx.delete(path)
    fail("private_oci_layer: " + message)

def _write_header(rctx, value):
    # Owner-only: the file holds a credential until the fetch ends.
    rctx.file(_HEADER, "", executable = False)
    result = rctx.execute(["chmod", "0600", _HEADER])
    if result.return_code != 0:
        _fail(rctx, "chmod %s failed: %s" % (_HEADER, result.stderr))
    rctx.file(_HEADER, "Authorization: %s\n" % value, executable = False)

def _curl(rctx, url, output, extra = []):
    # The credential is read from the header file, so it is in no argument or log.
    cmd = ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "3", "--header", "@" + _HEADER]
    for header in extra:
        cmd += ["--header", header]
    result = rctx.execute(cmd + ["--output", output, url], timeout = 900)
    if result.return_code != 0:
        _fail(rctx, "GET %s failed: %s" % (url, result.stderr))

def _sha256(rctx, path):
    result = rctx.execute(["sha256sum", path])
    if result.return_code != 0:
        _fail(rctx, "sha256sum %s failed: %s" % (path, result.stderr))
    return result.stdout.split(" ")[0]

def _private_oci_layer_impl(rctx):
    registry, repository = rctx.attr.registry, rctx.attr.repository
    base = "https://%s/v2/%s" % (registry, repository)

    _write_header(rctx, "Basic " + _docker_auth(rctx, registry))
    _curl(rctx, "https://%s/token?scope=repository:%s:pull&service=%s" % (registry, repository, registry), "token.json")
    token = json.decode(rctx.read("token.json")).get("token")
    rctx.delete("token.json")
    if not token:
        _fail(rctx, "the token response has no token")
    _write_header(rctx, "Bearer " + token)

    _curl(rctx, base + "/manifests/" + rctx.attr.digest, "manifest.json", [_ACCEPT])
    if "sha256:" + _sha256(rctx, "manifest.json") != rctx.attr.digest:
        _fail(rctx, "the manifest does not match %s" % rctx.attr.digest)
    layers = [layer["digest"] for layer in json.decode(rctx.read("manifest.json"))["layers"]]
    rctx.delete("manifest.json")
    if rctx.attr.layer not in layers:
        _fail(rctx, "%s does not list layer %s" % (rctx.attr.digest, rctx.attr.layer))

    _curl(rctx, base + "/blobs/" + rctx.attr.layer, "layer.tar.gz")
    rctx.delete(_HEADER)
    if "sha256:" + _sha256(rctx, "layer.tar.gz") != rctx.attr.layer:
        _fail(rctx, "the layer does not match %s" % rctx.attr.layer)

    # In this repository "//pkg" names its own packages; "@@//pkg" is the main one.
    visibility = ["@@" + v if v.startswith("//") else v for v in rctx.attr.visibility_to]
    rctx.file("BUILD.bazel", 'exports_files(["layer.tar.gz"], visibility = %s)\n' % repr(visibility))

private_oci_layer = repository_rule(
    implementation = _private_oci_layer_impl,
    attrs = {
        "digest": attr.string(mandatory = True, doc = "The image manifest digest, sha256:<hex>."),
        "layer": attr.string(mandatory = True, doc = "The layer digest to export, sha256:<hex>."),
        "registry": attr.string(mandatory = True),
        "repository": attr.string(mandatory = True),
        "visibility_to": attr.string_list(mandatory = True),
    },
    environ = ["DOCKER_CONFIG", "HOME"],
)
