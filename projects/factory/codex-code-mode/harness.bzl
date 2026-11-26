"""Import the executables from the deployed amd64 kagent adapter."""

def _import(ctx):
    index = ctx.path(ctx.attr.index)
    blobs = index.dirname.get_child("blobs").get_child("sha256")
    manifest = json.decode(ctx.read(index))["manifests"][0]
    layers = json.decode(ctx.read(blobs.get_child(manifest["digest"].split(":")[1])))["layers"]
    binaries = ["codex", "codex-code-mode-host", "codex-exec-server", "kagent-codex"]
    for layer in layers:
        blob = blobs.get_child(layer["digest"].split(":")[1])
        listing = ctx.execute(["tar", "tf", blob])
        if listing.return_code:
            fail(listing.stderr)
        members = [path for path in listing.stdout.split("\n") if path.lstrip("./") in ["usr/local/bin/" + b for b in binaries]]
        if members:
            result = ctx.execute(["tar", "xf", blob] + members)
            if result.return_code:
                fail(result.stderr)
    ctx.file("BUILD.bazel", "exports_files(%s)" % repr(["usr/local/bin/" + b for b in binaries]))

import_harness = repository_rule(
    implementation = _import,
    attrs = {"index": attr.label(mandatory = True)},
)
