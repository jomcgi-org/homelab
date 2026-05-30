load("@aspect_rules_py//py:defs.bzl", "py_library")

# Per-domain Iceberg table schemas, auto-discovered into the TABLES registry.
# pyiceberg import resolution comes from the # gazelle:resolve directives in the
# parent projects/lakehouse/iceberg/BUILD (inherited into this sub-package).
py_library(
    name = "tables",
    srcs = [
        "__init__.py",
        "gap_events.py",
        "note_events.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//pyiceberg",
    ],
)
