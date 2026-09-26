# gazelle:exclude
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:public"])

exports_files(["ci"])

py_library(
    name = "central_build_ratchet_lib",
    srcs = ["central_build_ratchet.py"],
    imports = ["."],
)

py_test(
    name = "central_build_ratchet_test",
    srcs = ["central_build_ratchet_test.py"],
    deps = [
        ":central_build_ratchet_lib",
        "@pip//pytest",
    ],
)

py_library(
    name = "chart_version_guard_lib",
    srcs = ["chart_version_guard.py"],
    imports = ["."],
)

py_test(
    name = "chart_version_guard_test",
    srcs = ["chart_version_guard_test.py"],
    deps = [
        ":chart_version_guard_lib",
        "@pip//pytest",
    ],
)

py_library(
    name = "test_source_reachability_lib",
    srcs = ["test_source_reachability.py"],
    imports = ["."],
)

py_test(
    # Keep the target name distinct from the test module. rules_python creates
    # a <target>.pytest_main package, which otherwise collides with a top-level
    # module named <target>.py during collection.
    name = "python_test_source_reachability_test",
    srcs = ["test_source_reachability_test.py"],
    deps = [
        ":test_source_reachability_lib",
        "@pip//pytest",
    ],
)

py_library(
    name = "verification_genrule_guard_lib",
    srcs = ["verification_genrule_guard.py"],
    imports = ["."],
)

py_test(
    name = "verification_genrule_guard_test",
    srcs = ["verification_genrule_guard_test.py"],
    deps = [
        ":verification_genrule_guard_lib",
        "@pip//pytest",
    ],
)

sh_test(
    name = "ci_test",
    srcs = ["ci_test.sh"],
    data = [
        "ci",
        "local-affected-test.sh",
        "//:buildbuddy.yaml",
    ],
)

sh_test(
    name = "affected_targets_test",
    srcs = ["affected_targets_test.sh"],
    data = ["affected-targets.sh"],
)

sh_test(
    name = "local_affected_test_test",
    srcs = ["local_affected_test_test.sh"],
    data = ["local-affected-test.sh"],
)
