"""oci_binaries - lift statically linked binaries out of a pulled OCI image.

Some upstreams publish a program ONLY as a container image: there is no release
tarball to `multiarch_http_archive` and no Wolfi package to name in an
apko.yaml. Apache Iggy is the motivating case (see ADR embervm/039): its server
ships to Docker Hub as `apache/iggy:<version>` and the GitHub-built tarballs are
30-day CI artifacts, not durable release assets.

This repository rule takes an OCI layout directory (from `oci.pull`, pinned by
digest), applies its layers, and copies out only the named paths, exposing them
as one filegroup. It is the thin sibling of `//bazel/tools/postgres:oci_postgres`
and deliberately does NOT copy a shared-library closure, a dynamic loader, or
generate exec wrappers: it is for binaries that need none of that.

That restriction is CHECKED, not assumed. Each extracted file is verified to be
a statically linked ELF (no PT_INTERP program header), so an upstream that
quietly switches to a dynamically linked build fails the fetch with a clear
message instead of producing an image whose binary dies at exec time with a
missing-loader error that surfaces only on a guest boot.

Example:

    oci_binaries(
        name = "iggy_server_bin",
        image = "@iggy_server_image_linux_amd64//:index.json",
        binaries = {"usr/local/bin/iggy-server": "usr/local/bin/iggy-server"},
    )

Then layer it into an image:

    tar(
        name = "iggy_tar",
        srcs = ["@iggy_server_bin//:binaries"],
        mtree = ["./usr/local/bin/iggy-server type=file ... mode=0755"],
    )
"""

_BUILD_FILE_CONTENT = """\
filegroup(
    name = "binaries",
    srcs = glob(["**/*"]),
    visibility = ["//visibility:public"],
)

# Individually addressable so an mtree entry can name one binary's path with
# $(execpath @repo//:usr/local/bin/foo); a filegroup of several files has no
# single execpath.
exports_files(glob(["**/*"]))
"""

# ELF program-header type PT_INTERP. Its presence names a dynamic loader, which
# means the binary is NOT self-contained and this rule is the wrong tool.
_PT_INTERP = 3

def _child_path(parent, rel):
    """Join a parent path and a relative path as a string.

    String concatenation rather than `get_child` so multi-segment relative paths
    work uniformly across Bazel versions (same reason as oci_postgres.bzl).

    Args:
        parent: A Bazel path object or string.
        rel: Relative path string, may contain '/'.

    Returns:
        String path.
    """
    return str(parent) + "/" + rel

def _layer_blob_paths(rctx, oci_layout_dir):
    """Resolve an OCI layout's layer blobs in application order.

    Reads index.json to find the manifest, then the manifest to find its layers.
    `oci.pull` with a single-platform `platforms` list writes a layout whose
    index names exactly that platform's manifest, so index.manifests[0] is the
    image we want and there is no platform selection to do here.

    Args:
        rctx: Repository context.
        oci_layout_dir: String path to the OCI layout directory.

    Returns:
        List of string paths to layer blobs, first layer first.
    """
    index = json.decode(rctx.read(rctx.path(_child_path(oci_layout_dir, "index.json"))))
    manifests = index.get("manifests", [])
    if not manifests:
        fail("OCI index.json has no manifests: " + oci_layout_dir)

    algo, hex_digest = manifests[0]["digest"].split(":", 1)
    manifest_blob = _child_path(oci_layout_dir, "blobs/" + algo + "/" + hex_digest)
    manifest = json.decode(rctx.read(rctx.path(manifest_blob)))

    layers = manifest.get("layers", [])
    if not layers:
        fail("OCI manifest has no layers: " + manifest_blob)

    paths = []
    for layer in layers:
        layer_algo, layer_hex = layer["digest"].split(":", 1)
        paths.append(_child_path(oci_layout_dir, "blobs/" + layer_algo + "/" + layer_hex))
    return paths

def _extract_layers(rctx, layer_paths, staging_dir):
    """Apply every layer into one staging directory, in order.

    Args:
        rctx: Repository context.
        layer_paths: Layer blob paths, first layer first.
        staging_dir: String path to unpack into.
    """
    for layer_path in layer_paths:
        result = rctx.execute(["tar", "xzf", layer_path, "-C", staging_dir], timeout = 300)
        if result.return_code != 0:
            # Not every layer is gzipped (an OCI layout may carry plain tar
            # layers); retry uncompressed before treating this as a failure.
            result = rctx.execute(["tar", "xf", layer_path, "-C", staging_dir], timeout = 300)
            if result.return_code != 0:
                fail("Failed to extract OCI layer {path}: {err}".format(
                    path = layer_path,
                    err = result.stderr,
                ))

def _assert_static_elf(rctx, path, rel):
    """Fail unless `path` is a statically linked ELF executable.

    Reads the ELF header and walks the program headers looking for PT_INTERP.
    A static binary has none; a dynamically linked one names its loader there
    and would need a shared-library closure this rule does not copy.

    Uses `od` to read the bytes, since a repository rule cannot read binary
    files directly (rctx.read decodes as text).

    Args:
        rctx: Repository context.
        path: String path to the extracted file.
        rel: The path as named in `binaries`, for error messages.
    """
    result = rctx.execute(["od", "-A", "n", "-t", "u1", "-v", "-N", "64", path], timeout = 30)
    if result.return_code != 0:
        fail("Could not read ELF header of {rel}: {err}".format(rel = rel, err = result.stderr))
    header = [int(b) for b in result.stdout.split()]
    if len(header) < 64:
        fail("{rel} is too short to be an ELF binary".format(rel = rel))
    if header[0:4] != [0x7F, 69, 76, 70]:  # \x7fELF
        fail("{rel} is not an ELF binary".format(rel = rel))
    if header[4] != 2:
        fail("{rel} is not a 64-bit ELF binary".format(rel = rel))

    # Little-endian 64-bit ELF header fields: e_phoff at 0x20 (8 bytes),
    # e_phentsize at 0x36 (2), e_phnum at 0x38 (2).
    e_phoff = _le(header, 0x20, 8)
    e_phentsize = _le(header, 0x36, 2)
    e_phnum = _le(header, 0x38, 2)
    if e_phnum == 0:
        fail("{rel} has no ELF program headers".format(rel = rel))

    span = e_phnum * e_phentsize
    result = rctx.execute(
        ["od", "-A", "n", "-t", "u1", "-v", "-j", str(e_phoff), "-N", str(span), path],
        timeout = 30,
    )
    if result.return_code != 0:
        fail("Could not read program headers of {rel}: {err}".format(rel = rel, err = result.stderr))
    phdrs = [int(b) for b in result.stdout.split()]
    for i in range(e_phnum):
        offset = i * e_phentsize
        if offset + 4 > len(phdrs):
            break
        if _le(phdrs, offset, 4) == _PT_INTERP:
            fail(("{rel} is DYNAMICALLY linked (it declares a PT_INTERP loader). " +
                  "oci_binaries only lifts self-contained static binaries; the " +
                  "upstream image must have changed its build. Either pin the " +
                  "previous digest or switch to a rule that copies the shared " +
                  "library closure (see //bazel/tools/postgres:oci_postgres).").format(rel = rel))

def _le(byts, offset, width):
    """Decode `width` little-endian bytes starting at `offset`.

    Args:
        byts: List of ints, each 0-255.
        offset: Start index.
        width: Number of bytes.

    Returns:
        The decoded integer.
    """
    value = 0
    for i in range(width):
        value += byts[offset + i] << (8 * i)
    return value

def _oci_binaries_impl(rctx):
    oci_layout_dir = str(rctx.path(rctx.attr.image).dirname)

    staging_dir = str(rctx.path("_staging"))
    rctx.execute(["mkdir", "-p", staging_dir], timeout = 10)
    _extract_layers(rctx, _layer_blob_paths(rctx, oci_layout_dir), staging_dir)

    repo_dir = str(rctx.path(""))
    for src_rel, dest_rel in rctx.attr.binaries.items():
        src = _child_path(staging_dir, src_rel)
        if rctx.execute(["test", "-f", src], timeout = 10).return_code != 0:
            fail("{src} is not present in the image {img}".format(
                src = src_rel,
                img = str(rctx.attr.image),
            ))
        _assert_static_elf(rctx, src, src_rel)

        dest = _child_path(repo_dir, dest_rel)
        rctx.execute(["mkdir", "-p", dest.rsplit("/", 1)[0]], timeout = 10)
        result = rctx.execute(["cp", "-a", src, dest], timeout = 60)
        if result.return_code != 0:
            fail("Failed to copy {src}: {err}".format(src = src_rel, err = result.stderr))
        rctx.execute(["chmod", "0755", dest], timeout = 10)

    rctx.execute(["rm", "-rf", staging_dir], timeout = 120)
    rctx.file("BUILD.bazel", _BUILD_FILE_CONTENT)

oci_binaries = repository_rule(
    implementation = _oci_binaries_impl,
    attrs = {
        "image": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "Label of index.json in an OCI layout from oci.pull, e.g. " +
                  "@iggy_server_image_linux_amd64//:index.json.",
        ),
        "binaries": attr.string_dict(
            mandatory = True,
            allow_empty = False,
            doc = "Map of image-relative source path (no leading slash) to the " +
                  "repo-relative destination path, e.g. " +
                  '{"usr/local/bin/iggy-server": "usr/local/bin/iggy-server"}. ' +
                  "Every source must be a statically linked ELF binary; the " +
                  "fetch fails otherwise.",
        ),
    },
    doc = "Lift statically linked binaries out of a digest-pinned OCI image, " +
          "for upstreams that publish only a container image. See the module " +
          "docstring and ADR embervm/039.",
)
