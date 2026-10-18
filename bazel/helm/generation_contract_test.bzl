"""Analysis checks for generated Applications beside hand-written renders."""

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

def _equal(description, actual, expected):
    if actual != expected:
        fail("{}: got {}, want {}".format(description, actual, expected))

def _generation_contract_test_impl(ctx):
    # These providers describe the expanded native rules, including the
    # argocd_app emitted by Gazelle into the real deploy package.
    chart = "projects/mcp/context-forge-gateway/chart"
    deploy = "projects/mcp/context-forge-gateway/deploy"
    chart_label = "//" + chart + ":chart"
    deploy_values = "//" + deploy + ":deploy_values"
    gke_values = "//" + deploy + ":gke_values"
    defaults = "//" + chart + ":values.yaml"

    base = ctx.attr.base[_Contract]
    gke = ctx.attr.gke[_Contract]
    template = ctx.attr.template[_Contract]
    _equal("base render inputs", base.srcs, [chart_label, deploy_values])
    _equal("GKE render inputs", gke.srcs, [chart_label, defaults, deploy_values, gke_values])
    for name, contract, values, output in [
        ("base", base, [":deploy_values"], "manifests/all.yaml"),
        ("GKE", gke, [defaults, ":deploy_values", ":gke_values"], "manifests_gke/all.yaml"),
    ]:
        expected_cmd = "$(location @multitool//tools/helm) template context-forge-gateway " + chart + " --namespace mcp --include-crds"
        for value in values:
            expected_cmd += " --values $(location {})".format(value)
        _equal(name + " render command", contract.cmd, expected_cmd + " > $@")
        if deploy + "/values.yaml" not in contract.src_files:
            fail(name + " render lost deploy_values' values.yaml")
        target = ctx.attr.base if name == "base" else ctx.attr.gke
        _equal(name + " output", [f.short_path for f in target[DefaultInfo].files.to_list()], [deploy + "/" + output])
    if deploy + "/values-gke.yaml" not in gke.src_files:
        fail("GKE render lost gke_values' values-gke.yaml")
    if deploy + "/values-gke.yaml" in base.src_files:
        fail("base render unexpectedly includes GKE values")

    _equal("template script", template.srcs, ["//bazel/helm:helm-template-test.sh"])
    _equal("template inputs", template.data[1:], [chart_label, defaults, "//" + deploy + ":values.yaml"])
    _equal("template arguments", template.args, [
        "$(rootpath @multitool//tools/helm)",
        "context-forge-gateway",
        chart,
        "mcp",
        "$(rootpath " + defaults + ")",
        "$(rootpath values.yaml)",
    ])

    # Real cross-project references force Bazel to check generated visibility.
    # The platform chart also has a same-package argocd_app consumer.
    for target in ctx.attr.charts:
        if not target[DefaultInfo].files.to_list():
            fail("empty generated chart: " + str(target.label))

    return _success(ctx)

def _admissibility_consumers_test_impl(ctx):
    data = ctx.attr.admissibility[_Contract].data
    for output in ["manifests/all.yaml", "manifests_gke/all.yaml"]:
        if "//projects/mcp/context-forge-gateway/deploy:" + output not in data:
            fail("admissibility gate lost " + output)
    return _success(ctx)

def _success(ctx):
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.write(executable, "#!/bin/sh\nexit 0\n", is_executable = True)
    return [DefaultInfo(executable = executable)]

helm_generation_contract_test = rule(
    implementation = _generation_contract_test_impl,
    test = True,
    attrs = {
        "base": attr.label(mandatory = True, aspects = [_observe]),
        "gke": attr.label(mandatory = True, aspects = [_observe]),
        "template": attr.label(mandatory = True, aspects = [_observe]),
        "charts": attr.label_list(default = [
            "//projects/monolith-agents/chart:chart",
            "//projects/operators/oci-model-cache/helm/oci-model-cache-operator:chart",
            "//projects/platform/priority-classes:chart",
        ]),
    },
)

helm_admissibility_consumers_test = rule(
    implementation = _admissibility_consumers_test_impl,
    test = True,
    attrs = {
        "admissibility": attr.label(mandatory = True, aspects = [_observe]),
    },
)
