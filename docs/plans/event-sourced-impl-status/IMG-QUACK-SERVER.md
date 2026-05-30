# IMG-QUACK-SERVER — Quack serving pod (Wavefront 3)

**Unit:** IMG-QUACK-SERVER (Wavefront-3 image unit)
**Classification:** [auto] — purely additive; one NEW package
`projects/lakehouse/quack-server/`, no existing-file edits.

## What shipped (all new files)

- `projects/lakehouse/quack-server/__init__.py` — package marker / role docstring.
- `projects/lakehouse/quack-server/server.py` — the serving pod:
  - `build_connection()` — startup: `duckdb_query.connect()` (loads httpfs/iceberg/vss
    - the SeaweedFS S3 secret) and, when `SERVING_ARTIFACT_URL` is set, ATTACHes the
      initial artifact so the pod can serve before the first swap.
  - `run_swap_consumer()` — durable NATS pull subscriber on
    `events.serving.artifact-ready` (durable `quack-serving-swap`). Each event is
    parsed (`parse_artifact_ready`) and applied via
    `duckdb_query.attach_or_replace_sql("notes", path)` — the zero-downtime hot-swap
    (platform/004: in-flight queries finish on the old snapshot, new queries see the
    new). Disposition: malformed → `term`; swap failure → leave for redelivery;
    success → `ack`.
  - FastAPI app (`create_app`): `GET /healthz` (liveness + current artifact version),
    `POST /search` (VSS nearest-neighbour via `vector_search_sql`, bearer-token gated
    when `QUACK_QUERY_TOKEN` is set).
  - `main()` — wires the swap consumer into the FastAPI lifespan and serves via uvicorn.
  - Config from env: `NATS_URL`, `SEAWEEDFS_S3_ENDPOINT`, `SERVING_ARTIFACT_URL`,
    `QUACK_QUERY_TOKEN`, `HOST`/`PORT`/`LOG_LEVEL`.
- `projects/lakehouse/quack-server/server_test.py` — hermetic (fake NatsClient +
  in-memory `duckdb.connect(':memory:')`, no remote extensions). Asserts the
  artifact-ready handler issues exactly the `ATTACH OR REPLACE` SQL the pure builder
  produces and acks; malformed messages are termed; `/healthz` returns ok + version;
  `/search` enforces the token and rejects bad `k`.
- `projects/lakehouse/quack-server/BUILD` — `py_library` (quack-server),
  `py_venv_binary` (main, `server.py`), `py3_image` (image), `py_test`, semgrep tests.

## Hot-swap consumer design

The swap is driven by a **durable NATS consumer the pod owns**, not by an HTTP call.
Only `BuildServingArtifactWorkflow` publishes `events.serving.artifact-ready`, and
NATS is internal-only — so the privileged-swap requirement from platform/004 §Security
("swap SQL travels Quack's privileged query path with a builder-scoped token") is
satisfied by **topology**: the swap never crosses the public HTTP boundary. The public
`POST /search` path is the only externally reachable surface and is gated by
`QUACK_QUERY_TOKEN` (the monolith issues per-client tokens for the web app read path).

A single shared DuckDB connection backs both the HTTP handlers and the swap loop;
`ATTACH OR REPLACE` is non-blocking on DuckDB 1.5.3, so no in-flight-query coordination
is needed (each query finishes on its starting snapshot). `version` is advisory metadata
surfaced by `/healthz`.

## py3_image target

```python
py3_image(
    name = "image",
    binary = "//projects/lakehouse/quack-server:main",
    env = {"PYTHONPATH": ".../main.runfiles/_main:.../main.runfiles/_main/projects/lakehouse/quack-server"},
    main = "server.py",
    repository = "ghcr.io/jomcgi/homelab/projects/lakehouse/quack-server",
)
```

Modelled field-for-field on the monolith `image`: multiarch (x86_64 + aarch64, the
macro default), non-root (uid 65532 — the `@python_base` default), PYTHONPATH set to the
workspace root + the package dir to match `imports = ["."]`.

## Conventions / deviations

- **Hyphenated package name.** The unit spec dir is `quack-server`, which a Python
  absolute import (`projects.lakehouse.quack_server`) cannot express. Like the trips
  image tools (`projects/trips/tools/publish-trip-images`), the package uses the
  monolith-style `imports = ["."]` (the package dir is a sys.path root) with flat
  intra-package imports (`import server`); cross-package deps
  (`projects.lakehouse.duckdb_query` / `.nats_client`) still resolve via the workspace
  root on PYTHONPATH. This is the only lakehouse package NOT using the absolute-import
  convention — forced by the hyphen.
- **gazelle resolve directives** in the BUILD: `duckdb @pip//duckdb` (the manifest
  regen was skipped in LIB-SCAFFOLD) and `server` → this package's own library.
- DuckDB pinned to 1.5.3 (the version platform/004 verified ATTACH OR REPLACE against)
  via the requirements lock (LIB-SCAFFOLD).

## CI / Test / Push-images status

_(filled in after the first CI run)_
