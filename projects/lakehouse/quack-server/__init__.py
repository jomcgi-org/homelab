"""Stateless DuckDB/Quack serving pod (ADR platform/004 §Hot-swap, §Architecture).

A serving pod holds the current serving artifact (`notes-vN.duckdb`) ATTACHed
in-RAM and answers vector/note queries over HTTP. It owns no durable state: the
artifact is rebuilt whole by ``BuildServingArtifactWorkflow`` and published to
SeaweedFS S3; the pod hears about a new version via the
``events.serving.artifact-ready`` NATS subject and hot-swaps it with
``ATTACH OR REPLACE`` (verified zero-downtime on DuckDB 1.5.3 — in-flight queries
finish on the old snapshot, new queries see the new).

Cloudflare CDN sits in front of the pod's HTTP API (per platform/004 §Read
paths); this package only implements the pod.
"""
