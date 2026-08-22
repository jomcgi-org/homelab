"""oci_postgres - Extract PostgreSQL binaries + pgvector from an OCI image.

This repository rule takes an OCI layout directory (from oci.pull) and extracts
only the PostgreSQL binaries, pgvector extension files, and shared library
dependencies needed to run PostgreSQL in integration tests.

It parses the OCI layout's index.json and manifest to locate layers, then
extracts them using standard tools (tar/gzip) — no crane dependency needed
in the repository rule context.
"""

# PostgreSQL binaries to extract (relative paths within the image filesystem)
_PG_BINARIES = [
    "usr/lib/postgresql/16/bin/postgres",
    "usr/lib/postgresql/16/bin/initdb",
    "usr/lib/postgresql/16/bin/pg_isready",
    "usr/lib/postgresql/16/bin/pg_ctl",
]

# pgvector extension files
_PGVECTOR_FILES = [
    "usr/lib/postgresql/16/lib/vector.so",
    "usr/share/postgresql/16/extension/vector.control",
]

_BUILD_FILE_CONTENT = """\
filegroup(
    name = "postgres",
    srcs = glob(["**/*"]),
    visibility = ["//visibility:public"],
)
"""

def _child_path(parent, rel):
    """Construct a child path string from a parent path and relative path.

    Uses string concatenation to avoid issues with get_child and
    multi-segment paths across Bazel versions.

    Args:
        parent: A Bazel path object or string.
        rel: Relative path string (may contain '/').

    Returns:
        String path.
    """
    return str(parent) + "/" + rel

def _parse_oci_layout(rctx, oci_layout_dir):
    """Parse an OCI layout directory to find layer blob paths.

    Reads index.json -> manifest -> layer digests, returning the
    filesystem paths to each layer blob within the OCI layout.

    Args:
        rctx: Repository context.
        oci_layout_dir: String path to the OCI layout directory.

    Returns:
        List of string paths to layer blob files (gzipped tarballs).
    """

    # Read index.json to find the manifest digest
    index_content = rctx.read(rctx.path(_child_path(oci_layout_dir, "index.json")))
    index = json.decode(index_content)

    manifests = index.get("manifests", [])
    if not manifests:
        fail("OCI index.json has no manifests")

    manifest_digest = manifests[0]["digest"]

    # Digest format is "sha256:abc123..." — convert to blob path
    algo, hex_digest = manifest_digest.split(":", 1)
    manifest_blob = _child_path(oci_layout_dir, "blobs/" + algo + "/" + hex_digest)

    # Read the manifest to find layer digests
    manifest_content = rctx.read(rctx.path(manifest_blob))
    manifest = json.decode(manifest_content)

    layers = manifest.get("layers", [])
    if not layers:
        fail("OCI manifest has no layers")

    layer_paths = []
    for layer in layers:
        layer_digest = layer["digest"]
        layer_algo, layer_hex = layer_digest.split(":", 1)
        layer_path = _child_path(oci_layout_dir, "blobs/" + layer_algo + "/" + layer_hex)
        layer_paths.append(layer_path)

    return layer_paths

def _extract_layers(rctx, layer_paths, output_dir):
    """Extract all OCI layers into a single output directory.

    Layers are applied in order (first to last), matching how container
    runtimes build the filesystem.

    Args:
        rctx: Repository context.
        layer_paths: List of string paths to layer blob files.
        output_dir: String path to extract into.
    """
    for layer_path in layer_paths:
        result = rctx.execute(
            ["tar", "xzf", layer_path, "-C", output_dir],
            timeout = 120,
        )

        # Some layers may use different compression or have whiteout files;
        # tar may return warnings but still succeed. Only fail on real errors.
        if result.return_code != 0:
            # Try without gzip in case it's an uncompressed tar
            result = rctx.execute(
                ["tar", "xf", layer_path, "-C", output_dir],
                timeout = 120,
            )
            if result.return_code != 0:
                fail("Failed to extract OCI layer: {path}\nstderr: {err}".format(
                    path = layer_path,
                    err = result.stderr,
                ))

def _copy_postgres_files(rctx, staging_dir):
    """Copy only the needed PostgreSQL files from the staging directory.

    Selectively copies PG binaries, pgvector extension files, and shared
    libraries — leaving behind the rest of the image filesystem.

    Args:
        rctx: Repository context.
        staging_dir: String path to the fully extracted image filesystem.
    """
    repo_dir = str(rctx.path(""))

    # Copy specific binaries and pgvector files
    for rel_path in _PG_BINARIES + _PGVECTOR_FILES:
        src = _child_path(staging_dir, rel_path)
        dest = _child_path(repo_dir, rel_path)

        # Ensure parent directory exists
        parent_dir = dest.rsplit("/", 1)[0]
        result = rctx.execute(["mkdir", "-p", parent_dir], timeout = 10)
        if result.return_code != 0:
            fail("Failed to create directory {dir}: {err}".format(
                dir = parent_dir,
                err = result.stderr,
            ))
        result = rctx.execute(["cp", "-a", src, dest], timeout = 10)
        if result.return_code != 0:
            fail("Failed to copy {path}: {err}".format(
                path = rel_path,
                err = result.stderr,
            ))

    # Copy pgvector SQL migration files (glob pattern: vector--*.sql)
    sql_src_dir = _child_path(staging_dir, "usr/share/postgresql/16/extension")
    sql_dest_dir = _child_path(repo_dir, "usr/share/postgresql/16/extension")
    rctx.execute(["mkdir", "-p", sql_dest_dir], timeout = 10)

    result = rctx.execute(
        ["find", sql_src_dir, "-maxdepth", "1", "-name", "vector--*.sql"],
        timeout = 10,
    )
    if result.return_code != 0:
        fail("Failed to find pgvector SQL files: {err}".format(err = result.stderr))
    sql_files = [f for f in result.stdout.strip().split("\n") if f]
    if not sql_files:
        fail("No pgvector SQL migration files found in {dir}".format(dir = sql_src_dir))
    for sql_file in sql_files:
        cp_result = rctx.execute(["cp", "-a", sql_file, sql_dest_dir + "/"], timeout = 10)
        if cp_result.return_code != 0:
            fail("Failed to copy pgvector SQL file {f}: {err}".format(
                f = sql_file,
                err = cp_result.stderr,
            ))

    # Copy the Debian shared libraries the PostgreSQL binaries and extension
    # modules actually link against, resolved with the Debian dynamic linker
    # itself. The CI host's glibc is older than Debian Bookworm's (2.36), so
    # even non-glibc libraries like libstdc++ need newer glibc symbols; the
    # wrappers in bin/ run everything through the Debian ld-linux with
    # --library-path so all of that comes from here, never from the host.
    #
    # This used to copy the WHOLE usr/lib/x86_64-linux-gnu (492 MB: libLLVM
    # twice at 129 MB for the JIT, 50 MB of perl for plperl, libz3, ...).
    # That directory is in the runfiles of EVERY bdd_test, and a remote test
    # execution materialises its whole input tree: 535 MB of postgres in a
    # 1.07 GB tree, times hundreds of test executions per full run
    # (invocation e4e74dd6, 2026-08-22). The closure is a small fraction.
    # If the loader cannot run here (macOS host), fall back to the full copy
    # so the repository still assembles.
    arch_lib_src = _child_path(staging_dir, "usr/lib/x86_64-linux-gnu")
    arch_lib_dest = _child_path(repo_dir, "usr/lib/x86_64-linux-gnu")
    result = rctx.execute(["test", "-d", arch_lib_src], timeout = 5)
    if result.return_code == 0:
        rctx.execute(["mkdir", "-p", arch_lib_dest], timeout = 10)
        needed = _resolve_lib_closure(rctx, staging_dir, arch_lib_src)
        if needed == None:
            result = rctx.execute(
                ["cp", "-a", "-R", arch_lib_src + "/.", arch_lib_dest + "/"],
                timeout = 120,
            )
            if result.return_code != 0:
                fail("Failed to copy arch lib directory: {err}".format(err = result.stderr))
        else:
            for rel in needed:
                src = _child_path(arch_lib_src, rel)
                dest = _child_path(arch_lib_dest, rel)
                rctx.execute(["mkdir", "-p", dest.rsplit("/", 1)[0]], timeout = 10)
                result = rctx.execute(["cp", "-a", src, dest], timeout = 30)
                if result.return_code != 0:
                    fail("Failed to copy {rel}: {err}".format(rel = rel, err = result.stderr))

    # Copy the Debian dynamic linker (ld-linux-x86-64.so.2).
    for ld_src_rel in ["lib64", "lib/x86_64-linux-gnu"]:
        ld_src_dir = _child_path(staging_dir, ld_src_rel)
        result = rctx.execute(
            ["find", ld_src_dir, "-maxdepth", "1", "-name", "ld-linux*"],
            timeout = 10,
        )
        if result.return_code == 0 and result.stdout.strip():
            ld_dest_dir = _child_path(repo_dir, ld_src_rel)
            rctx.execute(["mkdir", "-p", ld_dest_dir], timeout = 10)
            for ld_file in result.stdout.strip().split("\n"):
                if ld_file:
                    rctx.execute(["cp", "-a", ld_file, ld_dest_dir + "/"], timeout = 10)

    # Copy the postgresql lib directory (contains internal .so files)
    pg_lib_src = _child_path(staging_dir, "usr/lib/postgresql/16/lib")
    pg_lib_dest = _child_path(repo_dir, "usr/lib/postgresql/16/lib")
    rctx.execute(["mkdir", "-p", pg_lib_dest], timeout = 10)
    result = rctx.execute(["cp", "-a", "-R", pg_lib_src + "/.", pg_lib_dest + "/"], timeout = 30)
    if result.return_code != 0:
        fail("Failed to copy postgresql lib directory: {err}".format(err = result.stderr))

    # No JIT in the test server. llvmjit.so is the provider module postgres
    # dlopens lazily; without it `jit` silently reports unavailable and every
    # query runs interpreted, which is what a test wants anyway. Keeping it
    # would pull libLLVM (129 MB) and libz3 (23 MB) into the closure below,
    # and the bitcode directory (23 MB) is only ever read by that provider.
    for jit_rel in ["llvmjit.so", "llvmjit_types.bc", "bitcode"]:
        rctx.execute(["rm", "-rf", _child_path(pg_lib_dest, jit_rel)], timeout = 30)

    # Copy the postgresql share directory (timezone, locale, SQL configs)
    pg_share_src = _child_path(staging_dir, "usr/share/postgresql/16")
    pg_share_dest = _child_path(repo_dir, "usr/share/postgresql/16")
    rctx.execute(["mkdir", "-p", pg_share_dest], timeout = 10)
    result = rctx.execute(["cp", "-a", "-R", pg_share_src + "/.", pg_share_dest + "/"], timeout = 30)
    if result.return_code != 0:
        fail("Failed to copy postgresql share directory: {err}".format(err = result.stderr))

def _resolve_lib_closure(rctx, staging_dir, arch_lib_src):
    """Resolve the shared-library closure of the PG binaries and modules.

    Runs the Debian ld-linux from the staging tree with --list (the loader's
    own ldd) over every PostgreSQL binary and every .so under the postgres
    lib directory, so dlopen'd extension modules (vector.so, plpgsql.so, ...)
    are covered as well as the executables. Every listed library that lives
    under usr/lib/x86_64-linux-gnu is kept, together with each symlink hop
    down to the real file, since the loader resolves by SONAME symlink.

    Returns a sorted list of paths relative to arch_lib_src, or None when
    the loader cannot execute on this host (then the caller copies all).
    """
    ld = None
    for ld_rel in ["lib64/ld-linux-x86-64.so.2", "lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"]:
        candidate = _child_path(staging_dir, ld_rel)
        if rctx.execute(["test", "-f", candidate], timeout = 5).return_code == 0:
            ld = candidate
            break
    if ld == None:
        return None

    pg_lib = _child_path(staging_dir, "usr/lib/postgresql/16/lib")
    elves = [_child_path(staging_dir, b) for b in _PG_BINARIES]
    found = rctx.execute(
        ["find", pg_lib, "-maxdepth", "1", "-name", "*.so", "-type", "f"],
        timeout = 30,
    )
    if found.return_code == 0:
        # llvmjit.so is deleted from the copy (see the caller), so it must
        # not contribute libLLVM and libz3 to the closure either.
        elves += [f for f in found.stdout.strip().split("\n") if f and not f.endswith("/llvmjit.so")]

    library_path = arch_lib_src + ":" + pg_lib
    prefix = arch_lib_src + "/"
    needed = {}
    for elf in elves:
        result = rctx.execute([ld, "--library-path", library_path, "--list", elf], timeout = 60)
        if result.return_code != 0:
            # The loader exists but cannot run (foreign host): copy everything.
            return None
        for line in result.stdout.split("\n"):
            # "\tlibssl.so.3 => /.../usr/lib/x86_64-linux-gnu/libssl.so.3 (0x...)"
            if "=>" not in line:
                continue
            target = line.split("=>", 1)[1].strip().split(" ")[0]
            if not target.startswith(prefix):
                continue
            rel = target[len(prefix):]
            # Keep each hop of the symlink chain; `cp -a` copies links as links.
            for _ in range(8):
                needed[rel] = True
                link = rctx.execute(["readlink", _child_path(arch_lib_src, rel)], timeout = 5)
                if link.return_code != 0 or not link.stdout.strip():
                    break
                hop = link.stdout.strip()
                if hop.startswith("/"):
                    if not hop.startswith(prefix):
                        break
                    rel = hop[len(prefix):]
                else:
                    rel = _child_path(rel.rsplit("/", 1)[0], hop) if "/" in rel else hop
    return sorted(needed.keys())

def _create_pg_wrappers(rctx):
    """Create wrapper scripts for PG binaries to use the Debian ld-linux.

    Renames each real binary to <name>.bin and creates a shell wrapper
    that execs through the Debian dynamic linker with --library-path.
    This ensures child processes (e.g. initdb calling postgres -V) also
    use the Debian libraries, avoiding glibc version mismatches.

    Wrappers use paths relative to the script location so they work in
    both the repository directory and Bazel runfiles (which may remap paths).
    """
    repo_dir = str(rctx.path(""))

    # Find which ld-linux relative path exists
    ld_rel_path = None
    for ld_rel in ["lib64/ld-linux-x86-64.so.2", "lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"]:
        ld_path = _child_path(repo_dir, ld_rel)
        result = rctx.execute(["test", "-f", ld_path], timeout = 5)
        if result.return_code == 0:
            ld_rel_path = ld_rel
            break

    if not ld_rel_path:
        # No ld-linux found (e.g. macOS) — skip wrapper creation
        return

    bin_dir = _child_path(repo_dir, "usr/lib/postgresql/16/bin")

    # Wrapper uses SCRIPT_DIR to compute paths relative to the script.
    # From usr/lib/postgresql/16/bin/, the repo root is 5 dirs up.
    wrapper_template = "#!/bin/sh\n" + \
                       'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n' + \
                       'ROOT="$(cd "$SCRIPT_DIR/../../../../.." && pwd)"\n' + \
                       'exec "$ROOT/{ld}" --library-path "$ROOT/usr/lib/x86_64-linux-gnu:$ROOT/usr/lib/postgresql/16/lib:$ROOT/usr/lib" "$SCRIPT_DIR/{binary}.bin" "$@"\n'

    # Rename each binary and create wrapper
    binaries = ["postgres", "initdb", "pg_isready", "pg_ctl"]
    for binary in binaries:
        bin_path = _child_path(bin_dir, binary)
        real_path = _child_path(bin_dir, binary + ".bin")

        result = rctx.execute(["test", "-f", bin_path], timeout = 5)
        if result.return_code != 0:
            continue

        # Rename: binary -> binary.bin
        rctx.execute(["mv", bin_path, real_path], timeout = 5)

        # Create wrapper script with relative paths
        wrapper_content = wrapper_template.format(
            ld = ld_rel_path,
            binary = binary,
        )
        rctx.file(
            "usr/lib/postgresql/16/bin/" + binary,
            wrapper_content,
            executable = True,
        )

def _oci_postgres_impl(rctx):
    """Implementation of the oci_postgres repository rule."""

    # Resolve the OCI layout directory from the label.
    # The label points to index.json inside the OCI layout repo;
    # we use .dirname to get the layout directory itself.
    oci_layout_dir = str(rctx.path(rctx.attr.image).dirname)

    # Step 1: Parse OCI layout to find layer blobs
    layer_paths = _parse_oci_layout(rctx, oci_layout_dir)

    # Step 2: Extract all layers into a staging directory
    staging_dir = str(rctx.path("_staging"))
    rctx.execute(["mkdir", "-p", staging_dir], timeout = 10)
    _extract_layers(rctx, layer_paths, staging_dir)

    # Step 3: Copy only the PostgreSQL files we need
    _copy_postgres_files(rctx, staging_dir)

    # Step 4: Create wrapper scripts for PG binaries.
    # initdb internally runs `postgres -V` using find_my_exec() which
    # looks in the same directory as the calling binary. We must ensure
    # ALL PG binaries go through ld-linux, including child processes.
    # Solution: rename real binaries to .bin, create shell wrappers that
    # exec through ld-linux with the correct library path.
    _create_pg_wrappers(rctx)

    # Step 5: Clean up the staging directory
    rctx.execute(["rm", "-rf", staging_dir], timeout = 60)

    # Step 6: Write BUILD file
    rctx.file("BUILD.bazel", _BUILD_FILE_CONTENT)

oci_postgres = repository_rule(
    implementation = _oci_postgres_impl,
    attrs = {
        "image": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "Label pointing to index.json in the OCI layout from oci.pull (e.g. @pgvector_pg16_linux_amd64//:index.json).",
        ),
    },
    doc = """Extract PostgreSQL binaries and pgvector extension from an OCI image.

This repository rule parses an OCI layout directory (produced by oci.pull),
extracts all image layers, and copies out only the PostgreSQL binaries,
pgvector extension files, and shared library dependencies needed to run
PostgreSQL in integration tests.

The extracted files are exposed as a single filegroup target.

Example:

    oci_postgres(
        name = "postgres_test",
        image = "@pgvector_pg16_linux_amd64//:index.json",
    )

Then depend on it in tests:

    py_test(
        name = "my_e2e_test",
        data = ["@postgres_test//:postgres"],
    )
""",
)
