load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
load("//bazel/tools/oci:py3_image.bzl", "py3_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# NATS -> Temporal workflow dispatchers (ADR agents/016). Small adapters that
# subscribe to NATS subjects and translate domain events into Temporal
# `start_workflow` calls. New dispatcher = a new `<id>.py` file exporting a
# module-level `DISPATCHERS` list; the package loader (`all_dispatchers()`)
# aggregates them, so adding one needs NO shared-list edit.
#
# `temporalio` is in the pip lock (LIB-SCAFFOLD) but the generated gazelle module
# manifest (bazel/tools/python/gazelle_python.yaml, GENERATED / content-checked)
# was not regenerated to include it, so gazelle can't auto-resolve
# `import temporalio`. Resolve it explicitly here — the documented escape hatch,
# matching orchestrator/BUILD.
# gazelle:resolve py temporalio @pip//temporalio
# gazelle:resolve py temporalio.client @pip//temporalio

py_library(
    name = "dispatchers",
    srcs = [
        "__init__.py",
        "artifact_ready.py",
        "gap_ready.py",
        "run.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/lakehouse",
        "//projects/lakehouse/events",
        "//projects/lakehouse/orchestrator",
        "@pip//temporalio",
    ],
)

# Process entrypoint: `python -m projects.lakehouse.dispatchers.run`. The image
# runs run.py:_main(). One process subscribes to every discovered dispatcher's
# subject (there is no per-subject pod — ADR 016 Open Q2 "one dispatcher service
# handling many subscriptions").
py_venv_binary(
    name = "main",
    srcs = ["run.py"],
    main = "run.py",
    visibility = ["//:__subpackages__"],
    deps = [
        ":dispatchers",
        "//projects/lakehouse",
        "//projects/lakehouse/events",
        "//projects/lakehouse/orchestrator",
        "@pip//temporalio",
    ],
)

# Multiarch (x86_64 + aarch64), non-root (uid 65532 — the python_base default),
# modeled field-for-field on the lakehouse worker image
# (//projects/lakehouse/image). run.py uses absolute
# `from projects.lakehouse.dispatchers...` imports that resolve from the
# workspace root, so PYTHONPATH is just the macro's auto-computed workspace root
# (no per-package dir needed, unlike the hyphenated quack-server package).
# SSL_CERT_FILE points OpenSSL at certifi's bundle (the minimal base has no CA
# certs) so outbound TLS to Temporal/NATS works.
py3_image(
    name = "image",
    binary = "//projects/lakehouse/dispatchers:main",
    env = {
        "PYTHONPATH": "/projects/lakehouse/dispatchers/main.runfiles/_main",
        "SSL_CERT_FILE": "/projects/lakehouse/dispatchers/main.runfiles/_main/projects/lakehouse/dispatchers/.main/lib/python3.13/site-packages/certifi/cacert.pem",
    },
    main = "run.py",
    repository = "ghcr.io/jomcgi/homelab/projects/lakehouse/dispatchers",
)

py_test(
    name = "dispatchers_test",
    srcs = ["dispatchers_test.py"],
    deps = [
        ":dispatchers",
        "//projects/lakehouse/events",
        "@pip//pytest",
        "@pip//temporalio",
    ],
)

semgrep_target_test(
    name = "main_semgrep_test",
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":main",
)

semgrep_test(
    name = "dispatchers_test_semgrep_test",
    srcs = ["dispatchers_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
