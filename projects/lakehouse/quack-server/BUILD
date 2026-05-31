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
# own library. The nats_client submodule resolves to its own subpackage (not the
# top-level //projects/lakehouse), mirroring nats_client/BUILD's directives —
# otherwise gazelle maps it to //projects/lakehouse and NatsClient is missing
# from the binary's deps.
# gazelle:resolve py duckdb @pip//duckdb
# gazelle:resolve py server //projects/lakehouse/quack-server:quack-server
# gazelle:resolve py projects.lakehouse.nats_client //projects/lakehouse/nats_client:nats_client
# gazelle:resolve py projects.lakehouse.nats_client.client //projects/lakehouse/nats_client:nats_client
#
# duckdb_query/query.py (a transitive dep scanned by the generated
# main_semgrep_test) hardcodes the in-cluster SeaweedFS S3 endpoint as a
# deliberate cluster constant — exclude that rule from the generated semgrep
# targets, matching duckdb_query/BUILD.
# gazelle:semgrep_exclude_rules duckdb-bound-param-in-array-distance,no-hardcoded-k8s-service-url
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
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
        "@pip//pydantic",
        "@pip//uvicorn",
    ],
)

# Process entrypoint: `python -m server` / the image runs server.py:main().
py_venv_binary(
    name = "main",
    srcs = ["server.py"],
    main = "server.py",
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/lakehouse/duckdb_query",
        "//projects/lakehouse/nats_client",
        "@pip//duckdb",
        "@pip//fastapi",
        "@pip//pydantic",
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
    # Bundle the signed DuckDB extensions so the serving connection LOADs them
    # from /opt/duckdb_ext/ instead of downloading at runtime (the hardened image
    # has no CA bundle DuckDB's extension installer can use). Quack only strictly
    # needs httpfs (S3 reads) + vss (HNSW search), but load_extensions loads the
    # full set; bundling all five keeps the worker and quack images consistent.
    # See MODULE.bazel and duckdb_query.load_extensions.
    multiarch_tars = [
        "@duckdb_ext_avro//:tar",
        "@duckdb_ext_httpfs//:tar",
        "@duckdb_ext_iceberg//:tar",
        "@duckdb_ext_parquet//:tar",
        "@duckdb_ext_vss//:tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/projects/lakehouse/quack-server",
)

py_test(
    name = "server_test",
    srcs = ["server_test.py"],
    imports = ["."],
    deps = [
        "//projects/lakehouse/duckdb_query",
        "//projects/lakehouse/quack-server",
        "@pip//duckdb",
        "@pip//fastapi",
        "@pip//pytest",
    ],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    exclude_rules = [
        "no-hardcoded-k8s-service-url",
        "duckdb-bound-param-in-array-distance",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "server_test_semgrep_test",
    srcs = ["server_test.py"],
    exclude_rules = [
        "no-hardcoded-k8s-service-url",
        "duckdb-bound-param-in-array-distance",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_target_test(
    name = "main_semgrep_test",
    exclude_rules = [
        "no-hardcoded-k8s-service-url",
        "duckdb-bound-param-in-array-distance",
    ],
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":main",
)
