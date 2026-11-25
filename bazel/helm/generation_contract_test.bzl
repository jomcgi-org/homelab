"""Analysis check that the admissibility gate keeps a known consumer's renders."""

_Contract = provider(fields = ["srcs", "src_files", "data", "args", "cmd"])

def _label(target):
    return "//{}:{}".format(target.label.package, target.label.name)

def _observe_impl(_target, ctx):
    srcs = getattr(ctx.rule.attr, "srcs", [])
    return [_Contract(
        srcs = [_label(src) for src in srcs],
        src_files = sorted([file.short_path for src in srcs for file in src[DefaultInfo].files.to_list()]),
        data = [_label(dep) for dep in getattr(ctx.rule.attr, "data", [])],
        args = getattr(ctx.rule.attr, "args", []),
        cmd = getattr(ctx.rule.attr, "cmd", ""),
    )]

_observe = aspect(implementation = _observe_impl)

def _admissibility_consumers_test_impl(ctx):
    data = ctx.attr.admissibility[_Contract].data
    for output in ["manifests/all.yaml", "manifests_gke/all.yaml"]:
        if "//projects/platform/argocd:" + output not in data:
            fail("admissibility gate lost " + output)
    return _success(ctx)

def _success(ctx):
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.write(executable, "#!/bin/sh\nexit 0\n", is_executable = True)
    return [DefaultInfo(executable = executable)]

helm_admissibility_consumers_test = rule(
    implementation = _admissibility_consumers_test_impl,
    test = True,
    attrs = {
        "admissibility": attr.label(mandatory = True, aspects = [_observe]),
    },
)
