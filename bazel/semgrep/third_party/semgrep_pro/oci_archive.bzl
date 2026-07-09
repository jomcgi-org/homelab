"""oci_archive - Fetch OCI artifacts from GHCR and extract filesystem layers."""

_GHCR_REGISTRY = "ghcr.io"

_DEFAULT_BUILD_FILE_CONTENT = """\
filegroup(
    name = "files",
    srcs = glob(["**/*"]),
    visibility = ["//visibility:public"],
)
"""

def _get_github_token(rctx):
    """Get a GitHub token from environment variables.

    Checks GHCR_TOKEN first, then GITHUB_TOKEN.

    Returns:
        The token string, or None if neither variable is set.
    """
    token = rctx.os.environ.get("GHCR_TOKEN", "")
    if token:
        return token
    token = rctx.os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    return None

def _exchange_token(rctx, github_token, image):
    """Exchange a GitHub token for a GHCR bearer token.

    Args:
        rctx: Repository context.
        github_token: GitHub personal access token or GHCR token.
        image: Image path (without registry prefix).

    Returns:
        Bearer token string.
    """
    url = "https://{registry}/token?service={registry}&scope=repository:{image}:pull".format(
        registry = _GHCR_REGISTRY,
        image = image,
    )

    result = rctx.execute(
        ["curl", "-sf", "-u", "_:" + github_token, url],
        timeout = 30,
    )

    if result.return_code != 0:
        fail("Failed to exchange GitHub token for GHCR bearer token.\n" +
             "Image: {image}\n".format(image = image) +
             "curl exit code: {code}\n".format(code = result.return_code) +
             "stderr: {err}".format(err = result.stderr))

    response = json.decode(result.stdout)
    token = response.get("token")
    if not token:
        fail("GHCR token response missing 'token' field.\nResponse: {resp}".format(resp = result.stdout))
    return token

def _fetch_manifest(rctx, bearer_token, image, digest):
    """Fetch an OCI manifest by digest.

    Args:
        rctx: Repository context.
        bearer_token: GHCR bearer token.
        image: Image path (without registry prefix).
        digest: Manifest digest (sha256:...).

    Returns:
        Decoded manifest dict.
    """
    url = "https://{registry}/v2/{image}/manifests/{digest}".format(
        registry = _GHCR_REGISTRY,
        image = image,
        digest = digest,
    )

    result = rctx.execute(
        [
            "curl",
            "-sf",
            "-H",
            "Authorization: Bearer " + bearer_token,
            "-H",
            "Accept: application/vnd.oci.image.manifest.v1+json",
            url,
        ],
        timeout = 30,
    )

    if result.return_code != 0:
        fail("Failed to fetch OCI manifest.\n" +
             "Image: {image}\n".format(image = image) +
             "Digest: {digest}\n".format(digest = digest) +
             "curl exit code: {code}\n".format(code = result.return_code) +
             "stderr: {err}".format(err = result.stderr))

    return json.decode(result.stdout)

def _archive_extension(media_type):
    """Determine file extension from OCI layer mediaType.

    Handles both OCI media types (suffix +gzip/+zstd) and Docker media
    types (suffix .gzip). crane append produces Docker-style layers by
    default: application/vnd.docker.image.rootfs.diff.tar.gzip

    Args:
        media_type: OCI or Docker mediaType string.

    Returns:
        File extension including leading dot (e.g. ".tar.gz").
    """
    if "gzip" in media_type:
        return ".tar.gz"
    if "zstd" in media_type:
        return ".tar.zst"
    return ".tar"

def _download_layer(rctx, bearer_token, image, layer_digest, output):
    """Download a layer blob from GHCR.

    Uses curl with -L to follow redirects (GHCR redirects to CDN),
    which is necessary because repository_ctx.download() sends auth
    headers on redirect, causing CDN failures.

    Args:
        rctx: Repository context.
        bearer_token: GHCR bearer token.
        image: Image path (without registry prefix).
        layer_digest: Layer blob digest.
        output: Output file path.
    """
    url = "https://{registry}/v2/{image}/blobs/{digest}".format(
        registry = _GHCR_REGISTRY,
        image = image,
        digest = layer_digest,
    )

    result = rctx.execute(
        [
            "curl",
            "-sfL",
            "-H",
            "Authorization: Bearer " + bearer_token,
            "-o",
            output,
            url,
        ],
        timeout = 600,
    )

    if result.return_code != 0:
        fail("Failed to download OCI layer blob.\n" +
             "Image: {image}\n".format(image = image) +
             "Layer digest: {digest}\n".format(digest = layer_digest) +
             "curl exit code: {code}\n".format(code = result.return_code) +
             "stderr: {err}".format(err = result.stderr))

def _oci_archive_impl(rctx):
    """Implementation of the oci_archive repository rule."""

    digest = rctx.attr.digest

    # Empty digest means the artifact hasn't been published yet (e.g. new
    # platform or language pack). This is a build-time error — all digests
    # must be pinned before merging.
    if not digest:
        fail("oci_archive: empty digest for " + rctx.attr.image +
             " — all artifacts must have pinned digests. Run the update workflow.")

    # GHCR_TOKEN (or GITHUB_TOKEN) is required to fetch OCI artifacts.
    github_token = _get_github_token(rctx)
    if not github_token:
        fail("oci_archive: GHCR_TOKEN or GITHUB_TOKEN required to fetch " + rctx.attr.image +
             ". Set GHCR_TOKEN in your environment or .bazelrc.")

    image = rctx.attr.image

    # Step 1: Exchange GitHub token for GHCR bearer token
    bearer_token = _exchange_token(rctx, github_token, image)

    # Step 2: Fetch OCI manifest by digest
    manifest = _fetch_manifest(rctx, bearer_token, image, digest)

    # Steps 3-5: Download and extract EVERY layer, in order. An OCI image is the
    # union of its layers; a multi-layer engine image (e.g. one binary per layer)
    # would otherwise lose everything past the first. Single-layer artifacts (the
    # semgrep-pro engine, the rule packs) loop exactly once, so this is a no-op for
    # them. Later layers overlay earlier ones, matching image-assembly order.
    for i, layer in enumerate(manifest["layers"]):
        ext = _archive_extension(layer["mediaType"])
        archive_file = "layer_{}{}".format(i, ext)
        _download_layer(rctx, bearer_token, image, layer["digest"], archive_file)
        rctx.extract(
            archive = archive_file,
            stripPrefix = rctx.attr.strip_prefix,
        )
        rctx.delete(archive_file)

    # Step 6: Write BUILD file
    build_content = rctx.attr.build_file_content or _DEFAULT_BUILD_FILE_CONTENT
    rctx.file("BUILD.bazel", build_content)

oci_archive = repository_rule(
    implementation = _oci_archive_impl,
    attrs = {
        "image": attr.string(
            mandatory = True,
            doc = "GHCR image path without registry prefix (e.g. 'jomcgi/homelab/tools/semgrep-pro/engine-amd64').",
        ),
        "digest": attr.string(
            mandatory = True,
            doc = "OCI manifest digest (sha256:...). Must be pinned — empty digests are a build error.",
        ),
        "build_file_content": attr.string(
            doc = "Custom BUILD.bazel content for the extracted files. " +
                  "Defaults to a filegroup exposing all files.",
        ),
        "strip_prefix": attr.string(
            doc = "Directory prefix to strip from the extracted archive.",
        ),
    },
    environ = ["GITHUB_TOKEN", "GHCR_TOKEN"],
    doc = """Fetch a single OCI artifact from GHCR and extract its filesystem layer.

This repository rule implements a minimal OCI Distribution client:
1. Exchanges a GitHub token for a GHCR bearer token
2. Fetches the OCI manifest by digest
3. Downloads every layer blob (using curl -L to follow CDN redirects)
4. Extracts the layers in order and generates a BUILD file

Both GHCR_TOKEN (or GITHUB_TOKEN) and a pinned digest are required.
Missing credentials or empty digests are build errors.

Example:

    oci_archive(
        name = "semgrep_pro_engine_amd64",
        image = "jomcgi/homelab/tools/semgrep-pro/engine-amd64",
        digest = "sha256:abc123...",
        build_file_content = '''
            filegroup(
                name = "engine",
                srcs = ["osemgrep-pro"],
                visibility = ["//visibility:public"],
            )
        ''',
    )
""",
)
