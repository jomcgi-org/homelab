# Quack serving pod (ADR platform/004 §Hot-swap / §Architecture).
#
# This package is hyphenated (`quack-server`), so — like the trips image tools —
# it uses the monolith-style `imports = ["."]` (the package dir is a sys.path
# root) and flat intra-package imports (`import server`), NOT the lakehouse
# absolute-import convention which a hyphen cannot express. Cross-package deps
# (`projects.lakehouse.duckdb_query` / `.nats_client`) still resolve via the
# workspace root on PYTHONPATH.
#
# Resolve directives: gazelle's module manifest does not yet carry `duckdb`
# (LIB-SCAFFOLD added the wheel to the lock but skipped the manifest regen, see
# duckdb_query/BUILD), and the flat `server` import must map to this package's
# own library.
# gazelle:resolve py duckdb @pip//duckdb
# gazelle:resolve py server //projects/lakehouse/quack-server:quack-server
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/oci:py3_image.bzl", "py3_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "quack-server",
    srcs = [
        "__init__.py",
        "server.py",
    ],
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/lakehouse/duckdb_query",
        "//projects/lakehouse/nats_client",
        "@pip//duckdb",
        "@pip//fastapi",
        "@pip//uvicorn",
    ],
)

# Process entrypoint: `python -m server` / the image runs server.py:main().
py_venv_binary(
    name = "main",
    srcs = ["server.py"],
    imports = ["."],
    main = "server.py",
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/lakehouse/duckdb_query",
        "//projects/lakehouse/nats_client",
        "@pip//duckdb",
        "@pip//fastapi",
        "@pip//uvicorn",
    ],
)

# Multiarch (x86_64 + aarch64), non-root (uid 65532 — the python_base default),
# modelled field-for-field on the monolith `image` target. PYTHONPATH mirrors the
# monolith: workspace root + the package dir, matching `imports = ["."]`.
py3_image(
    name = "image",
    binary = "//projects/lakehouse/quack-server:main",
    env = {
        "PYTHONPATH": "/projects/lakehouse/quack-server/main.runfiles/_main:/projects/lakehouse/quack-server/main.runfiles/_main/projects/lakehouse/quack-server",
    },
    main = "server.py",
    repository = "ghcr.io/jomcgi/homelab/projects/lakehouse/quack-server",
)

py_test(
    name = "server_test",
    srcs = ["server_test.py"],
    imports = ["."],
    deps = [
        "//projects/lakehouse/quack-server",
        "@pip//duckdb",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "server_semgrep_test",
    srcs = ["server.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "server_test_semgrep_test",
    srcs = ["server_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
