"""Action inputs for execution-based repository compatibility checks."""

load("@aspect_rules_py//py:defs.bzl", "py_test")

def _manifest_impl(ctx):
    paths = {}
    files = []
    for target, key in ctx.attr.inputs.items():
        target_files = target[DefaultInfo].files.to_list()
        if len(target_files) != 1:
            fail("Expected one file for %s" % target.label)
        paths[key] = target_files[0].short_path
        files.extend(target_files)
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(out, json.encode(paths))
    return [DefaultInfo(files = depset([out]), runfiles = ctx.runfiles(files = files))]

_manifest = rule(
    implementation = _manifest_impl,
    attrs = {"inputs": attr.label_keyed_string_dict(allow_files = True)},
)

def compatibility_test(name, repo, arches, archive = True, assets = False, metadata = False, goose = False, executable = True):
    """Build every public label and compare the resulting files in Linux."""
    inputs = {
        "@%s//:binary" % repo: "binary",
        "@%s//:tar" % repo: "tar",
    }
    for arch in arches:
        inputs["@%s//:%s" % (repo, arch)] = arch
        inputs["@%s//:tar_%s" % (repo, arch)] = "tar_" + arch
        if goose:
            inputs["@http_goose_source_%s//file" % arch] = "source_" + arch
        else:
            inputs["fixtures/%s.%s" % (arch, "tar" if archive else "bin")] = "source_" + arch
    if metadata:
        inputs["@%s_metadata//:checked.txt" % repo] = "metadata"
    _manifest(name = name + "_manifest", inputs = inputs)
    py_test(
        name = name,
        srcs = ["compatibility_test.py"],
        main = "compatibility_test.py",
        data = [":" + name + "_manifest"],
        env = {
            "HTTP_MANIFEST": "$(rootpath :%s_manifest)" % name,
            "HTTP_ARCHES": ",".join(arches),
            "HTTP_ARCHIVE": str(archive),
            "HTTP_ASSETS": str(assets),
            "HTTP_GOOSE": str(goose),
            "HTTP_EXECUTABLE": str(executable),
        },
        target_compatible_with = ["@platforms//os:linux"],
    )
