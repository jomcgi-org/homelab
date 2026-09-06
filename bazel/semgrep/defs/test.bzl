"""Bazel test rules for running semgrep scans via semgrep-core."""

load("@rules_shell//shell:sh_test.bzl", "sh_test")

def semgrep_test(
        name,
        srcs,
        rules,
        lockfiles = [],
        sca_rules = [],
        exclude_rules = [],
        pro_engine = "//bazel/semgrep/third_party/semgrep_pro:engine",
        **kwargs):
    """Creates a cacheable test that runs semgrep-core Pro against source files.

    Invokes the OCI-vendored semgrep-core-proprietary binary with interfile
    analysis (-pro_inter_file), bypassing the Python pysemgrep wrapper. Results
    are cached by Bazel based on input file hashes — only re-runs when sources
    or rules change.

    The semgrep-core binary is discovered at runtime via find(1) in the
    runfiles tree, rather than passed as an argument, because Bazel's
    $(rootpath) can't resolve platform-specific select() targets in sh_test
    args. GHCR_TOKEN is required for fetching the engine.

    Args:
        name: Name of the test target
        srcs: Source files to scan (labels)
        rules: Semgrep rule config files or filegroups (labels)
        lockfiles: Lockfiles for SCA dependency scanning (e.g., go.sum, package-lock.json).
            When provided, these are passed after a second "--" separator so the
            test runner can feed them to semgrep-core for supply-chain analysis.
        sca_rules: Semgrep Supply Chain rule configs for dependency scanning.
            These rules target lockfile patterns and are merged with the regular
            rules list when invoking semgrep-core.
        exclude_rules: List of semgrep rule IDs to skip (e.g., ["no-privileged"])
        pro_engine: Label for the semgrep-core-proprietary binary filegroup.
            Defaults to the pro engine.
        **kwargs: Additional arguments passed to sh_test
    """
    env = kwargs.pop("env", {})
    if exclude_rules:
        env["SEMGREP_EXCLUDE_RULES"] = ",".join(exclude_rules)
    tags = kwargs.pop("tags", [])

    data = [
        "//bazel/semgrep/third_party/semgrep:engine",
        "//bazel/semgrep/defs:wrapper_support",
    ] + rules + sca_rules + srcs + lockfiles

    if pro_engine:
        data.append(pro_engine)

    rule_args = ["$(rootpaths {})".format(r) for r in rules + sca_rules]
    src_args = ["$(rootpaths {})".format(s) for s in srcs]
    lockfile_args = ["$(rootpaths {})".format(lf) for lf in lockfiles] if lockfiles else []

    sh_test(
        name = name,
        srcs = ["//bazel/semgrep/defs:semgrep-test.sh"],
        args = rule_args + ["--"] + src_args + (["--"] + lockfile_args if lockfile_args else []),
        data = data,
        env = env,
        tags = tags,
        **kwargs
    )

def semgrep_manifest_test(
        name,
        chart,
        chart_files,
        release_name,
        namespace,
        values_files,
        rules = ["//bazel/semgrep/rules:kubernetes_rules"],
        exclude_rules = [],
        pro_engine = "//bazel/semgrep/third_party/semgrep_pro:engine",
        **kwargs):
    """Creates a test that renders Helm manifests and scans them with semgrep-core.

    Combines helm template rendering with semgrep-core scanning. Fails if either
    rendering fails or semgrep finds violations. Results are cached by Bazel.

    Args:
        name: Name of the test target
        chart: Path to chart directory (e.g., "charts/todo")
        chart_files: Label for chart's filegroup (e.g., "//projects/todo/chart:chart")
        release_name: Helm release name
        namespace: Kubernetes namespace for rendering
        values_files: List of values file labels in order
        rules: Semgrep rule config files (default: kubernetes rules)
        exclude_rules: List of semgrep rule IDs to skip (e.g., ["no-privileged"])
        pro_engine: Label for the semgrep-core-proprietary binary filegroup.
            Defaults to the pro engine.
        **kwargs: Additional arguments passed to sh_test
    """
    env = kwargs.pop("env", {})
    if exclude_rules:
        env["SEMGREP_EXCLUDE_RULES"] = ",".join(exclude_rules)
    tags = kwargs.pop("tags", [])

    data = [
        "//bazel/semgrep/third_party/semgrep:engine",
        "//bazel/semgrep/defs:wrapper_support",
        "@multitool//tools/helm",
        chart_files,
    ] + rules + values_files

    if pro_engine:
        data.append(pro_engine)

    sh_test(
        name = name,
        srcs = ["//bazel/semgrep/defs:semgrep-manifest-test.sh"],
        args = [
                   "$(rootpath @multitool//tools/helm)",
                   release_name,
                   chart,
                   namespace,
               ] + ["$(rootpaths {})".format(r) for r in rules] +
               ["--"] +
               ["$(rootpath {})".format(vf) for vf in values_files],
        data = data,
        env = env,
        tags = tags,
        **kwargs
    )
