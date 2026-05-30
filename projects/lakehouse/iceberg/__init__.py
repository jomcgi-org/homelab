"""PyIceberg writer helpers + per-domain table definitions (ADR platform/004).

This package is the *write path* of the lakehouse (platform/004 §Write path):
events drained from NATS are committed as rows into per-domain Iceberg tables on
a SeaweedFS-backed S3 warehouse. It defines, standalone:

  catalog        catalog_config() / load_warehouse_catalog() for the SeaweedFS
                 warehouse (SqlCatalog + SQLite metadata DB — see catalog.py)
  writer         rows_to_arrow() / append_events() — dict rows -> pyarrow ->
                 Iceberg append
  tables/        per-domain pyiceberg Schema objects, auto-discovered via
                 pkgutil into the TABLES registry (table_name -> Schema)

The Iceberg schemas here intentionally duplicate the envelope fields described in
ADR agents/017 rather than importing the sibling ``events`` Pydantic models: a
pyiceberg ``Schema`` is a different representation (field IDs, nullability,
column types) and keeping it standalone lets this unit ship independently of the
``events`` package. Slight field duplication is accepted and intended.
"""
