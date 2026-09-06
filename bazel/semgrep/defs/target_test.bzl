"""Bazel test rule for running semgrep against a target's transitive sources."""

load("//bazel/semgrep/defs:aspect.bzl", "SemgrepSourcesInfo", "semgrep_source_aspect")

def _semgrep_target_test_impl(ctx):
    info = ctx.attr.target[SemgrepSourcesInfo]
    sources = info.sources.to_list()

    # Collect rule config files
    rule_files = []
    for rule_target in ctx.attr.rules:
        rule_files.extend(rule_target.files.to_list())

    # Collect lockfile files
    lockfile_files = []
    for lf_target in ctx.attr.lockfiles:
        lockfile_files.extend(lf_target.files.to_list())

    # Collect SCA rule files
    sca_rule_files = []
    for sca_target in ctx.attr.sca_rules:
        sca_rule_files.extend(sca_target.files.to_list())

    # Build environment variable exports
    env_lines = []
    if ctx.attr.exclude_rules:
        env_lines.append("export SEMGREP_EXCLUDE_RULES=\"{}\"".format(
            ",".join(ctx.attr.exclude_rules),
        ))

    # Build args: <rule-files> <sca-rule-files> -- <source-files> [-- <lockfile-files>]
    test_runner = ctx.file._test_runner

    args = [f.short_path for f in rule_files + sca_rule_files]
    args.append("--")
    args.extend([f.short_path for f in sources])
    if lockfile_files:
        args.append("--")
        args.extend([f.short_path for f in lockfile_files])

    # Write launcher script
    launcher = ctx.actions.declare_file(ctx.label.name + ".sh")
    lines = ["#!/usr/bin/env bash", "set -euo pipefail"]
    lines.extend(env_lines)

    # Let the wrapper distinguish lockfile-only work, explicit exclusions and
    # unexpected empty input. An empty source closure alone is not a clean scan.
    quoted_args = " ".join(["\"{}\"".format(a) for a in args])
    lines.append("exec \"{}\" {}".format(test_runner.short_path, quoted_args))

    ctx.actions.write(
        output = launcher,
        content = "\n".join(lines) + "\n",
        is_executable = True,
    )

    # Build runfiles — include all files the test needs at runtime.
    # Engine and pro_engine are filegroups whose files live in
    # DefaultInfo.files, not default_runfiles, so we must add both.
    engine_files = ctx.attr._engine[DefaultInfo].files.to_list()
    pro_files = ctx.attr.pro_engine[DefaultInfo].files.to_list() if ctx.attr.pro_engine else []
    support_files = ctx.attr._wrapper_support[DefaultInfo].files.to_list()
    all_files = [test_runner] + support_files + rule_files + sca_rule_files + sources + lockfile_files + engine_files + pro_files
    runfiles = ctx.runfiles(files = all_files)

    runfiles = runfiles.merge(ctx.attr._engine[DefaultInfo].default_runfiles)
    if ctx.attr.pro_engine:
        runfiles = runfiles.merge(ctx.attr.pro_engine[DefaultInfo].default_runfiles)

    return [DefaultInfo(executable = launcher, runfiles = runfiles)]

_semgrep_target_test = rule(
    implementation = _semgrep_target_test_impl,
    test = True,
    attrs = {
        "target": attr.label(
            aspects = [semgrep_source_aspect],
            mandatory = True,
            doc = "Target whose transitive sources will be scanned.",
        ),
        "rules": attr.label_list(
            allow_files = [".yaml"],
            mandatory = True,
            doc = "Semgrep rule config files or filegroups.",
        ),
        "exclude_rules": attr.string_list(
            doc = "Semgrep rule IDs to skip (matched against YAML filename).",
        ),
        "lockfiles": attr.label_list(
            allow_files = True,
            doc = "Lockfile(s) for SCA dependency scanning (e.g., go.sum, requirements.txt).",
        ),
        "sca_rules": attr.label_list(
            allow_files = True,
            doc = "SCA advisory rule config files or filegroups.",
        ),
        "pro_engine": attr.label(
            doc = "Label for semgrep-core-proprietary binary. Enables --pro flag.",
        ),
        "_test_runner": attr.label(
            default = "//bazel/semgrep/defs:semgrep-test.sh",
            allow_single_file = True,
        ),
        "_wrapper_support": attr.label(default = "//bazel/semgrep/defs:wrapper_support"),
        "_engine": attr.label(default = "//bazel/semgrep/third_party/semgrep:engine"),
    },
)

def semgrep_target_test(name, target, rules, lockfiles = [], sca_rules = [], exclude_rules = [], pro_engine = "//bazel/semgrep/third_party/semgrep_pro:engine", **kwargs):
    """Creates a test that scans a target's transitive sources with semgrep.

    Uses an aspect to walk the target's dependency graph and collect all source
    files from the main repository (excluding external deps). Runs semgrep
    once on the full source closure, enabling meaningful --pro cross-file analysis.

    Args:
        name: Name of the test target.
        target: Label of the target to scan (e.g., a py_venv_binary).
        rules: Semgrep rule config files or filegroups.
        lockfiles: Lockfile(s) for SCA dependency scanning (e.g., go.sum, requirements.txt).
        sca_rules: SCA advisory rule config files or filegroups.
        exclude_rules: List of semgrep rule IDs to skip.
        pro_engine: Optional label for semgrep-core-proprietary binary.
        **kwargs: Additional arguments passed to the test rule.
    """
    _semgrep_target_test(
        name = name,
        target = target,
        rules = rules,
        lockfiles = lockfiles,
        sca_rules = sca_rules,
        exclude_rules = exclude_rules,
        pro_engine = pro_engine,
        tags = kwargs.pop("tags", []),
        **kwargs
    )
