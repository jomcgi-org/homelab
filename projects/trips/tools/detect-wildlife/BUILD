load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "detect-wildlife",
    srcs = [
        "__init__.py",
        "main.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//open_gopro",
        "@pip//typer",
    ],
)

py_venv_binary(
    name = "main",
    srcs = ["main.py"],
    main = "main.py",
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//open_gopro",
        "@pip//typer",
    ],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_target_test(
    name = "main_semgrep_test",
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":main",
)

# gazelle:resolve py main //projects/trips/tools/detect-wildlife:detect-wildlife
# gazelle:resolve py main.CaptureQueue //projects/trips/tools/detect-wildlife:detect-wildlife
# gazelle:resolve py main.CaptureRecord //projects/trips/tools/detect-wildlife:detect-wildlife
# gazelle:resolve py main.DownloadStatus //projects/trips/tools/detect-wildlife:detect-wildlife
# gazelle:resolve py main.GracefulShutdown //projects/trips/tools/detect-wildlife:detect-wildlife
# gazelle:resolve py main.PerfStats //projects/trips/tools/detect-wildlife:detect-wildlife
py_test(
    name = "wildlife_test",
    srcs = ["wildlife_test.py"],
    imports = ["."],
    deps = [
        "//projects/trips/tools/detect-wildlife",
        "@pip//pytest",
    ],
)

semgrep_test(
    name = "wildlife_test_semgrep_test",
    srcs = ["wildlife_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "detect_wildlife_test_semgrep_test",
    srcs = ["detect_wildlife_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

# gazelle:resolve py main.download_worker //projects/trips/tools/detect-wildlife:detect-wildlife
py_test(
    name = "detect_wildlife_test",
    srcs = ["detect_wildlife_test.py"],
    imports = ["."],
    deps = [
        "//projects/trips/tools/detect-wildlife",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

# gazelle:resolve py gopro_test //projects/trips/tools/detect-wildlife:detect-wildlife
py_test(
    name = "gopro_test",
    srcs = ["gopro_test.py"],
    imports = ["."],
    deps = [
        "//projects/trips/tools/detect-wildlife",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

semgrep_test(
    name = "gopro_test_semgrep_test",
    srcs = ["gopro_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

# gazelle:resolve py main_test //projects/trips/tools/detect-wildlife:detect-wildlife
py_test(
    name = "main_test",
    srcs = ["main_test.py"],
    imports = ["."],
    deps = [
        "//projects/trips/tools/detect-wildlife",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

semgrep_test(
    name = "main_test_semgrep_test",
    srcs = ["main_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
