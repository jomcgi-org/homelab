# Trips into Monolith Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Finish folding the standalone `trips` service into the monolith: recover lost point data from S3, add a private authenticated ingestion endpoint, serve trips read-only via public SSR, move imgproxy into the monolith chart, repoint the publish tool, and decommission the standalone service.

**Architecture:** Postgres (`trips` schema) becomes the single source of truth; NATS is dropped entirely. The read-write monolith hosts a private ingestion `POST` (EXIF extraction server-side, writes Postgres + a new `monolith-trips` SeaweedFS bucket), reachable remotely via Cloudflare Access service token and locally via `kubectl port-forward`. The public read-only tier (`monolith-public` on `monolith-pg-ro`) serves trips as SSR-rendered pages with **zero internet-exposed JSON API**, mirroring the `ships` snapshot pattern. imgproxy is ported into the monolith chart as a plain Deployment+Service, restricted to the new bucket.

**Tech Stack:** Python/FastAPI/SQLModel, SvelteKit (SSR), Helm, Gateway API HTTPRoute, SeaweedFS S3 (boto3), imgproxy v3, Bazel/BuildBuddy CI, Atlas migrations.

---

## Testing model (read this first)

Per `.claude/CLAUDE.md`, there is **no local test loop**. Mac runners are not provisioned and the linux fallback is too slow. So for every task below:

- Write the test **first** (TDD), implement, then **commit**.
- Do **not** run `bazel test` / `pytest` locally. Verification is the end-of-plan CI run on the pushed branch (`gh pr checks <number> --watch`).
- Knowledge/monolith Python tests use SQLite + `create_all`, not migrations, so models must mirror any CHECK/array semantics in `__table_args__` (already handled in the existing trips models).
- When you change a numeric/string constant a test asserts on, `grep` the test tree for the old value in the same commit.

Each task lists the test to author and the expected CI assertion. "Run" steps are written for CI, not your workstation.

---

## Design decisions already settled (do not re-litigate)

1. **NATS is removed.** Postgres is source of truth. No queue, no replay-on-boot, no live WebSocket. The client (`publish-trip-images`) keeps retry logic and POSTs synchronously.
2. **New bucket `monolith-trips`, provisioned via GitOps** in the seaweedfs platform chart's `s3.createBuckets` (the chart's native declarative-bucket hook), not auto-create-on-write and not COSI. The monolith writes here; the legacy `trips` bucket is read-only input for recovery, deleted only at decommission. Images stay content-addressed (`img_<hex>.jpg`) so re-POSTing the same photo is idempotent.
3. **Zero public JSON API.** The read route exists but stays off `httproute-public.yaml`; only SvelteKit SSR (same tier) calls it. Public ingress exposes `/`, `/_app/`, and the trips image path only.
4. **Ingestion is private.** It lives on the read-write monolith, fronted by Cloudflare Access (service token for the remote device; `kubectl port-forward` to bypass Cloudflare at home).
5. **imgproxy only** (not nginx). Presets move into a small frontend URL helper; imgproxy is locked to the new bucket via `IMGPROXY_ALLOWED_SOURCES`.
6. **Recovery caveat accepted:** anything that existed only as a past NATS message and is not in EXIF, `config.yaml`, or an available KML (hand-added tags, gap points without source KML) is gone. The 4,877 photos and their geo/time/optics come back fully.

---

## Reference map (existing code to model on)

| Need                                                     | Reference                                                                                                                   |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Domain `register(app)` contract                          | `projects/monolith/ships/__init__.py`                                                                                       |
| SSR JSON snapshot route (kept off public ingress)        | `projects/monolith/ships/router.py` (`get_snapshot`, `_SNAPSHOT_CACHE_CONTROL`)                                             |
| Public app entrypoint                                    | `projects/monolith/app/main_public.py`                                                                                      |
| Public import guard + FORBIDDEN_MODULES                  | `projects/monolith/app/main_public_imports_test.py`                                                                         |
| Public reader GRANT (wholly-public schema)               | `projects/monolith/chart/migrations/20260618000000_dr_jobs_public_reader_grant.sql`                                         |
| DB session dependency                                    | `projects/monolith/app/db.py` (`get_session`)                                                                               |
| Canonical SeaweedFS boto3 client + write-with-autocreate | `projects/monolith/stars/grid.py` (`_s3_client`), `projects/monolith/chat/store.py` (`_blob_s3_put`)                        |
| Existing trips data layer                                | `projects/monolith/trips/models.py`, `trips/backfill/{main,exif,transform}.py`, migration `20260615120000_trips_schema.sql` |
| Trips BUILD targets + image-glob excludes                | `projects/monolith/BUILD` (`:15` gazelle exclude; trips globs at `:60,75,139,155,245,250,392`; targets at `:3629-3704`)     |
| Monolith chart deployment shape                          | `projects/monolith/chart/templates/deployment.yaml`, `_helpers.tpl`, `Chart.yaml`                                           |
| imgproxy values + presets to port                        | `projects/trips/chart/values.yaml:26-90`, `projects/trips/chart/templates/nginx-configmap.yaml`                             |
| Tool to repoint (NATS publish site)                      | `projects/trips/tools/publish-trip-images/main.py` (`js.publish("trips.point", ...)` at lines 773/1455/1665)                |

---

## Task 1: Extract a shared EXIF/transform core the endpoint and backfill both use

The backfill already has the EXIF + transform logic, but it lives under `trips/backfill/` which is **excluded from the server image**. The ingestion endpoint (in the server image) needs the same logic. Move the pure, dependency-light pieces into a server-image-safe module; keep S3/CLI orchestration in `backfill/`.

**Files:**

- Create: `projects/monolith/trips/exif.py` (moved from `trips/backfill/exif.py`)
- Create: `projects/monolith/trips/transform.py` (moved from `trips/backfill/transform.py`)
- Modify: `projects/monolith/trips/backfill/main.py` (import from `trips.exif` / `trips.transform`)
- Delete: `projects/monolith/trips/backfill/exif.py`, `projects/monolith/trips/backfill/transform.py`
- Move tests: `projects/monolith/trips/exif_test.py`, `projects/monolith/trips/transform_test.py`
- Modify: `projects/monolith/BUILD` (new `trips/*.py` go into the server lib; tests retargeted)

**Step 1: Move the modules and tests.** `git mv` the four files up one level; update the in-file imports in `backfill/main.py` from `from trips.backfill import exif, transform` (or relative) to `from trips import exif, transform`. `exif.py` deps: `pillow` (PIL). `transform.py` deps: `defusedxml` (KML parsing). Both are otherwise stdlib.

**Step 2: BUILD wiring.** In `projects/monolith/BUILD`:

- Add `trips/exif.py` and `trips/transform.py` to the `monolith_backend`/`:main` server library srcs, and add `pillow` + `defusedxml` to that library's deps if not already present (check first; chat/knowledge may already pull pillow).
- Keep `trips/backfill/**` in the excludes (it still owns boto3/typer/httpx orchestration).
- Retarget `trips_backfill_exif_test` → `trips_exif_test` and `trips_backfill_transform_test` → `trips_transform_test`, deps now `:monolith_backend` (or a focused `:trips_lib`).
- Remember the `# gazelle:exclude trips` at BUILD:15 means trips test/binary targets are hand-maintained (per `feedback_knowledge_gazelle_exclude` convention for knowledge; same applies here). Edit BUILD by hand; do not expect gazelle to generate these.

**Step 3: Author/keep the EXIF + transform unit tests** (they already exist; just ensure they pass under the new path). Expected CI: `trips_exif_test` and `trips_transform_test` PASS.

**Step 4: Commit.**

```bash
git add projects/monolith/trips/ projects/monolith/BUILD
git commit -m "refactor(trips): hoist exif/transform into server-image-safe modules"
```

---

## Task 2: Server-side EXIF ingestion service function (pure, testable)

A single function that takes image bytes + metadata and returns a `TripPoint`-shaped dict. Both the HTTP endpoint (Task 3) and a thin recovery wrapper (Task 5) call it.

**Files:**

- Create: `projects/monolith/trips/ingest.py`
- Test: `projects/monolith/trips/ingest_test.py`
- Modify: `projects/monolith/BUILD` (add `trips/ingest.py` to server lib; add `trips_ingest_test`)

**Step 1: Write the failing test.**

```python
# projects/monolith/trips/ingest_test.py
from pathlib import Path
from trips.ingest import build_point

def test_build_point_extracts_geo_time_optics(tmp_path):
    # a tiny JPEG fixture with GPS EXIF lives in testdata/
    img = (Path(__file__).parent / "testdata" / "geotagged.jpg").read_bytes()
    point = build_point(
        trip_slug="2025-liard-hot-springs",
        image_bytes=img,
        image_key="img_deadbeef.jpg",
        source="gopro",
        tags=["wildlife"],
        tz="America/Vancouver",
    )
    assert point["trip_slug"] == "2025-liard-hot-springs"
    assert point["id"] == "deadbeef"          # derived from image_key
    assert -180 <= point["lng"] <= 180 and -90 <= point["lat"] <= 90
    assert point["taken_at"] is not None       # tz-aware
    assert point["image"] == "img_deadbeef.jpg"
    assert point["source"] == "gopro"
    assert "wildlife" in point["tags"]

def test_build_point_rejects_missing_gps(tmp_path):
    img = (Path(__file__).parent / "testdata" / "no_gps.jpg").read_bytes()
    import pytest
    with pytest.raises(ValueError, match="no GPS"):
        build_point(trip_slug="t", image_bytes=img, image_key="img_x.jpg",
                    source="gopro", tags=[], tz="America/Vancouver")
```

Add two tiny JPEG fixtures under `projects/monolith/trips/testdata/` (one geotagged, one not). Generate them with Pillow + piexif offline and commit the bytes.

**Step 2: Implement `build_point`.**

```python
# projects/monolith/trips/ingest.py
"""Pure EXIF -> TripPoint construction shared by the HTTP endpoint and recovery."""
import tempfile
from datetime import datetime
from pathlib import Path

from trips import exif, transform


def build_point(*, trip_slug, image_bytes, image_key, source, tags, tz):
    """Extract a TripPoint-shaped dict from raw image bytes. Raises ValueError on no GPS."""
    with tempfile.NamedTemporaryFile(suffix=".jpg") as fh:
        fh.write(image_bytes)
        fh.flush()
        lat, lng, taken_iso, optics = exif.extract_exif(Path(fh.name))
    if lat is None or lng is None or not transform.is_valid_coordinates(lat, lng):
        raise ValueError("image has no GPS coordinates")
    taken_at = transform.localize(taken_iso, tz, fallback=datetime.now())
    point = {
        "trip_slug": trip_slug,
        "id": transform.point_id_from_image_key(image_key),
        "lat": round(lat, 5),
        "lng": round(lng, 5),
        "taken_at": taken_at,
        "image": image_key,
        "source": source,
        "tags": list(tags or []),
        "elevation": None,
    }
    if optics and not optics.is_empty():
        point.update(
            light_value=optics.light_value, iso=optics.iso,
            shutter_speed=optics.shutter_speed, aperture=optics.aperture,
            focal_length_35mm=optics.focal_length_35mm,
        )
    return point
```

Note: elevation is left `None` at ingest time (live points get it lazily or via a follow-up; the recovery path can still enrich via the backfill's `_fetch_elevations`). Keep it out of the hot ingestion path to avoid a synchronous NRCan call per upload.

**Step 3: BUILD + commit.**

```bash
git add projects/monolith/trips/ingest.py projects/monolith/trips/ingest_test.py \
        projects/monolith/trips/testdata/ projects/monolith/BUILD
git commit -m "feat(trips): server-side EXIF point builder shared by ingest and recovery"
```

---

## Task 3: Private ingestion HTTP endpoint (writes Postgres + new bucket)

**Files:**

- Create: `projects/monolith/trips/ingest_router.py`
- Create: `projects/monolith/trips/s3.py` (the shared SeaweedFS client + put-with-autocreate, modeled on `chat/store.py:_blob_s3_put`)
- Test: `projects/monolith/trips/ingest_router_test.py`
- Modify: `projects/monolith/trips/__init__.py` (add `register`)
- Modify: `projects/monolith/app/main.py` (import trips + `trips.register(app)`)
- Modify: `projects/monolith/BUILD`

**Step 1: Failing endpoint test** (FastAPI `TestClient`, SQLite session override, monkeypatched S3 put).

```python
# projects/monolith/trips/ingest_router_test.py
def test_post_image_writes_point_and_uploads(client, session, fake_s3, geotagged_jpg):
    resp = client.post(
        "/api/trips/ingest",
        params={"trip": "2025-liard-hot-springs", "source": "gopro"},
        files={"image": ("frame.jpg", geotagged_jpg, "image/jpeg")},
        headers={"X-Trips-Ingest-Key": "test-key"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"]
    # row landed
    from trips.models import TripPoint
    rows = session.exec(select(TripPoint)).all()
    assert len(rows) == 1 and rows[0].image == f"img_{body['id']}.jpg"
    # bytes landed in the new bucket, content-addressed
    assert fake_s3.put_calls[0]["bucket"] == "monolith-trips"

def test_post_image_requires_auth(client, geotagged_jpg):
    resp = client.post("/api/trips/ingest", params={"trip": "t"},
                       files={"image": ("f.jpg", geotagged_jpg, "image/jpeg")})
    assert resp.status_code == 401

def test_reingest_same_image_is_idempotent(client, session, fake_s3, geotagged_jpg):
    for _ in range(2):
        client.post("/api/trips/ingest", params={"trip": "t", "source": "gopro"},
                    files={"image": ("f.jpg", geotagged_jpg, "image/jpeg")},
                    headers={"X-Trips-Ingest-Key": "test-key"})
    from trips.models import TripPoint
    assert len(session.exec(select(TripPoint)).all()) == 1   # content-addressed PK
```

**Step 2: Implement the S3 helper** (`trips/s3.py`), copying the `stars/grid.py` client construction and `chat/store.py` autocreate-on-`NoSuchBucket`. Keep `import boto3` inside the function and tag the client line `# nosemgrep` (the `boto3-endpoint-url-missing-scheme` rule). Bucket name from `TRIPS_S3_BUCKET` env (default `monolith-trips`). Content-addressing: `key = f"img_{sha256(bytes)[:12]}.jpg"` to match the legacy hex-id scheme; dedupe with a `head_object` guard before `put_object`.

**Step 3: Implement the router.**

```python
# projects/monolith/trips/ingest_router.py
import hashlib, os
from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from sqlmodel import Session
from app.db import get_session
from trips import ingest, s3
from trips.models import Trip, TripPoint

router = APIRouter(prefix="/api/trips", tags=["trips-ingest"])

def _require_key(x_trips_ingest_key: str | None = Header(default=None)):
    expected = os.environ.get("TRIPS_INGEST_KEY", "")
    if not expected or x_trips_ingest_key != expected:
        raise HTTPException(status_code=401, detail="invalid ingest key")

@router.post("/ingest", status_code=201, dependencies=[Depends(_require_key)])
async def ingest_image(trip: str, source: str = "gopro", tags: str = "",
                       image: UploadFile = File(...),
                       session: Session = Depends(get_session)):
    data = await image.read()
    image_key = f"img_{hashlib.sha256(data).hexdigest()[:12]}.jpg"
    point = ingest.build_point(
        trip_slug=trip, image_bytes=data, image_key=image_key,
        source=source, tags=[t for t in tags.split(",") if t],
        tz=_trip_tz(session, trip),
    )
    s3.put_image(image_key, data, content_type=image.content_type or "image/jpeg")
    session.merge(TripPoint(**point))   # upsert on (trip_slug, id)
    session.commit()
    return {"id": point["id"], "image": image_key}
```

`_trip_tz` looks up `Trip.tz` for the slug, defaulting to `America/Vancouver`. The defence-in-depth note: the endpoint is private (not on `httproute-public.yaml`) AND key-gated AND Cloudflare-Access-gated. The X-key guards the local `port-forward` path that bypasses Cloudflare.

**Step 4: Wire `register`.**

```python
# projects/monolith/trips/__init__.py  (add)
def register(app):
    from trips.ingest_router import router as ingest_router
    from trips.read_router import router as read_router   # created in Task 4
    app.include_router(ingest_router)
    app.include_router(read_router)
```

Add to `app/main.py`: `import trips` and `trips.register(app)` next to `ships.register(app)`.

**Step 5: BUILD + commit.** `trips/ingest_router.py` and `trips/s3.py` auto-glob into `:main`/`:monolith_backend` (they match `trips/**/*.py` and are not under `trips/backfill/**`), so no lib-srcs change is needed for the private image. Hand-add a `trips_ingest_router_test` target (gazelle excludes trips) with deps on `:monolith_backend` and `@pip//pytest` + FastAPI test deps. The endpoint ships only in the private `app.main`; do not touch the public libs here (that is Task 4).

```bash
git add projects/monolith/trips/ projects/monolith/app/main.py projects/monolith/BUILD
git commit -m "feat(trips): private authenticated ingestion endpoint writing Postgres + monolith-trips bucket"
```

---

## Task 4: Public SSR read router + register_public + grant + un-fence from public closure

**Files:**

- Create: `projects/monolith/trips/read_router.py`
- Create: `projects/monolith/chart/migrations/<ts>_trips_public_reader_grant.sql`
- Modify: `projects/monolith/chart/migrations/atlas.sum` (regenerated by Atlas tooling; see Task 4 step 4)
- Modify: `projects/monolith/trips/__init__.py` (add `register_public`)
- Modify: `projects/monolith/app/main_public.py` (`import trips`, `trips.register_public(app)`)
- Modify: `projects/monolith/app/main_public_imports_test.py` (narrow FORBIDDEN_MODULES)
- Modify: `projects/monolith/public_reader_grants_test.py` (add trips schema)
- Modify: `projects/monolith/BUILD` (un-exclude trips read modules from server image globs)

**Step 1: Read router** modeled on `ships/router.py`. SSR-only JSON, CDN cache headers, kept **off** `httproute-public.yaml`.

```python
# projects/monolith/trips/read_router.py
from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, select
from app.db import get_session
from trips.models import Trip, TripPoint

router = APIRouter(prefix="/api/trips", tags=["trips"])
_CACHE = "public, max-age=300, s-maxage=86400"   # match ships' snapshot policy; see frontend cache-headers.js

@router.get("/trip/{slug}")
def get_trip(slug: str, response: Response, session: Session = Depends(get_session)):
    trip = session.get(Trip, slug)
    if trip is None:
        return Response(status_code=404)
    points = session.exec(
        select(TripPoint).where(TripPoint.trip_slug == slug).order_by(TripPoint.taken_at)
    ).all()
    response.headers["Cache-Control"] = _CACHE
    return {"trip": trip, "points": points}
```

Add a `/trips` index route returning the list of trips for the default-redirect landing. (Keep the read router and the ingest router under the same `/api/trips` prefix but in separate modules so the public closure imports only the read module.)

**Step 2: register_public** (lazy import of ONLY the read router so boto3/pillow/EXIF deps stay out of the public import closure).

```python
# projects/monolith/trips/__init__.py  (add)
def register_public(app):
    from trips.read_router import router as read_router
    app.include_router(read_router)
```

Add `import trips` + `trips.register_public(app)` to `app/main_public.py`.

**Step 3: Narrow the public import guard.** In `app/main_public_imports_test.py`, remove the blanket `"trips"` from `FORBIDDEN_MODULES` and replace with the private/heavy submodules: `"trips.ingest_router"`, `"trips.ingest"`, `"trips.s3"`, `"trips.backfill"`. This enforces that `register_public` never drags the write path into the public app. Expected CI: `main_public_imports_test` PASS (proves the read closure is clean).

**Step 4: Grant migration** (model on the dr_jobs grant; trips is wholly public, no view needed).

```sql
-- projects/monolith/chart/migrations/<ts>_trips_public_reader_grant.sql
GRANT USAGE ON SCHEMA trips TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA trips TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA trips
    GRANT SELECT ON TABLES TO public_reader;
```

Use a timestamp after `20260618...`. Regenerate `atlas.sum` via the repo's Atlas hashing step (the migrations dir is hashed; a stale sum fails CI). Extend `public_reader_grants_test.py` to include `trips`. Reminder from `feedback_public_reader_grant_new_schema`: without this grant the public page 503s.

**Step 5: Un-fence trips READ modules from the PUBLIC image only.** The private libs (`:main` / `:monolith_backend`, BUILD:60) already glob `trips/**/*.py` and exclude only `trips/backfill/**`, so the read router, models, and ingest modules already ship privately, no change there. The work is on the public side:

- In both the `monolith_public_backend` (BUILD:283-296) and `main_public` (BUILD:335-348) `glob` include lists, add `"trips/**/*.py"`.
- In `_PUBLIC_PRUNE_EXCLUDE` (BUILD:243-279), **remove the blanket `"trips/**"`** (line 255) and add narrow excludes so only the read path enters the public closure:
  ```python
  "trips/backfill/**",     # already present, keep
  "trips/ingest_router.py", # private write endpoint
  "trips/ingest.py",        # EXIF builder (pulls pillow)
  "trips/s3.py",            # boto3 writer
  "trips/exif.py",          # pillow
  "trips/transform.py",     # defusedxml (only the write/backfill path needs it)
  ```
  This leaves `trips/__init__.py`, `trips/models.py`, and `trips/read_router.py` in the public image. `read_router.py` must NOT import `ingest`/`s3`/`exif`/`transform` (it only reads Postgres), or the public closure breaks at import time, which `main_public_imports_test` will catch.
- Do NOT add `pillow`/`defusedxml` to `monolith_public_backend` deps (the public read path does not need them; boto3 is already there but unused by the read path).
- The `# gazelle:exclude trips` (BUILD:15) stays (targets remain hand-maintained).

**Step 6: Commit.**

```bash
git add projects/monolith/trips/ projects/monolith/app/main_public.py \
        projects/monolith/app/main_public_imports_test.py \
        projects/monolith/public_reader_grants_test.py \
        projects/monolith/chart/migrations/ projects/monolith/BUILD
git commit -m "feat(trips): public SSR read router, public_reader grant, un-fence read modules"
```

---

## Task 5: Recovery: extend backfill to copy bytes into the new bucket, then run it

The backfill currently reads the legacy `trips` bucket and writes `trips.points`, but leaves images in the old bucket. New serving reads `monolith-trips`, so recovery must copy the bytes across.

**Files:**

- Modify: `projects/monolith/trips/backfill/main.py` (add `--dest-bucket` copy step, reuse `trips.ingest.build_point` so recovery and live ingest share one code path)
- Test: `projects/monolith/trips/backfill/main_test.py` (or extend existing transform/exif coverage with a copy unit)

**Step 1: Failing test** for the copy step: given a source bucket key list and a fake S3, recovery `put`s each object into `dest_bucket` with the same key, skipping keys already present (idempotent re-run).

**Step 2: Implement** a `_copy_images(s3, src_bucket, dest_bucket, keys)` using `copy_object` (server-side, same SeaweedFS endpoint) with a `head_object` skip guard; wire a `--dest-bucket monolith-trips` option into `run(...)`. Refactor the point construction inside `run` to call `trips.ingest.build_point` so there is exactly one EXIF path.

**Step 3: Commit.**

```bash
git add projects/monolith/trips/backfill/
git commit -m "feat(trips): backfill copies images into monolith-trips bucket via shared ingest path"
```

**Step 4: Run recovery (operational, after the image ships).** This is a manual op, not CI:

```bash
# from a machine with cluster access; port-forward Postgres + SeaweedFS, or run in-pod
bazel run //projects/monolith:trips_backfill -- run \
    --slug 2025-liard-hot-springs \
    --config projects/trips/frontend/public/trips/2025-liard-hot-springs/config.yaml \
    --src-bucket trips --dest-bucket monolith-trips \
    --kml <path-to-saved-kml-if-any>   # restores gap points; omit if no KML
```

Repeat per trip slug under `projects/trips/frontend/public/trips/*/config.yaml`. Verify `SELECT count(*) FROM trips.points;` matches expected (~4,877 photo points + any gap points) and that `monolith-trips` lists the same object count as `trips`.

---

## Task 6: Provision the bucket via GitOps + imgproxy into the monolith-PUBLIC chart, reusing the img.jomcgi.dev tunnel route

**Topology correction (discovered mid-implementation):** `jomcgi.dev` is served by a SEPARATE chart, `projects/monolith-public/chart/` (OCI, its own ArgoCD app + namespace `monolith-public`). The private `monolith` chart's `httproute-public.yaml` is dormant (`cfIngress.public.enabled: false`). monolith-public's HTTPRoute is deliberately frontend-only (ADR 004). Public images today are served via `img.jomcgi.dev`, routed at the **Cloudflare tunnel layer** (`projects/platform/cloudflare-gateway/values-prod.yaml`) straight to `trips-nginx`, bypassing the envoy gateway. So imgproxy belongs in the **monolith-public** chart, and the public image route is the existing **img.jomcgi.dev tunnel entry repointed** at it (NOT a path on the envoy HTTPRoute, which would break ADR 004's frontend-only invariant).

**Files:**

- Modify: `projects/platform/seaweedfs/values.yaml` (add `monolith-trips` under `filer.s3.createBuckets`, enabling `filer.s3`)
- Create: `projects/monolith-public/chart/templates/imgproxy.yaml` (Deployment + Service in the monolith-public namespace; match the chart's homelab-library style if it uses one, else plain YAML)
- Modify: `projects/monolith-public/chart/values.yaml` (imgproxy block)
- Modify: `projects/platform/cloudflare-gateway/values-prod.yaml` (repoint `img.jomcgi.dev` from `trips-nginx.trips.svc` to `monolith-public-imgproxy.monolith-public.svc.cluster.local:8080`)
- Modify: `projects/monolith-public/chart/Chart.yaml` (bump `version`) and `projects/monolith-public/deploy/application.yaml` (`targetRevision`), keep in sync per `feedback_chart_version_bumps`.
- REVERT the Task 6 v1 changes wrongly placed in the private monolith chart (imgproxy.yaml, imgproxy values block, the `/trips/img/` httproute rule, the `Chart.yaml`/`application.yaml` bump).

**Step 0: Provision the bucket declaratively.** The seaweedfs createBuckets hook reads `.Values.filer.s3.createBuckets` (gated on `filer.s3.enabled`), NOT a top-level `s3.createBuckets`. Add to `projects/platform/seaweedfs/values.yaml` under `filer.s3`:

```yaml
    s3:
      enabled: true
      createBuckets:
        - name: monolith-trips
          anonymousRead: true
```

Enabling `filer.s3` also starts an embedded S3 listener on the filer pod, but the canonical `seaweedfs-s3` Service selector keys on `app.kubernetes.io/component: s3` (the standalone component), so imgproxy's endpoint is unaffected; buckets live in shared filer metadata so `monolith-trips` is visible via the standalone endpoint. seaweedfs is git-HEAD sourced, so no chart bump. The hook is `post-install` only (PostSync), so a pre-existing cluster may need one manual `s3.bucket.create` or rely on the `trips/s3.py` autocreate fallback; both are fine.

**Step 1: imgproxy Deployment+Service in the monolith-public chart.** Read `projects/monolith-public/chart/templates/deployment.yaml` and `values.yaml` first to match its style (it runs `web` + `frontend` via the homelab library). Add imgproxy (component `imgproxy`), `darthsim/imgproxy:v3.25.0`, port 8080:

- `IMGPROXY_USE_S3=true`, `IMGPROXY_S3_ENDPOINT=http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333`, `IMGPROXY_S3_REGION=us-east-1`, anonymous AWS creds.
- **Lock to the bucket:** `IMGPROXY_ALLOWED_SOURCES=s3://monolith-trips/`.
- `IMGPROXY_ALLOW_INSECURE_URLS=true` (unsafe URL mode; safe because ALLOWED_SOURCES locks the bucket).
- `IMGPROXY_QUALITY=90`, `IMGPROXY_FORMAT_QUALITY=webp=92,avif=90,jpeg=90`, `IMGPROXY_ENABLE_WEBP_DETECTION=true`, `IMGPROXY_STRIP_METADATA=true`, `IMGPROXY_MAX_SRC_RESOLUTION=50`.
- non-root uid 65532, `runAsNonRoot: true`, drop ALL caps, `readOnlyRootFilesystem: true` + a writable `/tmp` emptyDir, `/health` liveness+readiness probes.
- Resources: CPU request only (no CPU limit, per `feedback_resource_sizing_convention`), mem request==limit (128Mi/512Mi).

**Step 2: Repoint the tunnel route.** In `projects/platform/cloudflare-gateway/values-prod.yaml`, change the `img.jomcgi.dev` mapping from `http://trips-nginx.trips.svc.cluster.local:80` to `http://monolith-public-imgproxy.monolith-public.svc.cluster.local:8080`. This keeps the public image surface on the same hostname (no envoy HTTPRoute change, ADR 004 intact) and means decommissioning trips-nginx does not break images. Frontend image URLs become absolute `https://img.jomcgi.dev/...` (cross-origin `<img>` tags need no CORS).

**Step 3: Bump monolith-public chart version + targetRevision; commit.** The plain `<img src>` cross-origin works without CORS (unlike fetch+traceparent, see `feedback_otel_fetch_cors_thirdparty`).

---

## Task 7: SvelteKit SSR frontend (port React pages to Svelte)

The standalone frontend is React/wouter on Cloudflare Pages. Port the three pages to SvelteKit SSR under the monolith public routes. This is the largest task; split commits per page. Use `ships` (`ShipsMap.svelte`, maplibre GeoJSON layer) as the maplibre reference; trips maps should likewise render points as a GPU GeoJSON layer, not DOM markers.

**Public URL (settled):** `jomcgi.dev/app/trips/<slug>`, matching ships/stars/hikes/dr-jobs. The old `trips.jomcgi.dev` is let die (no redirect). Pages live under `routes/public/app/trips/` (the `hooks.js` reroute maps the apex under `/public`, so `jomcgi.dev/app/trips/...` -> `routes/public/app/trips/...`). Served by the **monolith-public** frontend pod via the catch-all `/` rule, so NO HTTPRoute change is needed for pages.

**Files (create under `projects/monolith/frontend/src/routes/public/app/trips/`):**

- `+page.server.js` (default: redirect to or render latest trip) and `[slug]/+page.server.js` (SSR load).
- `[slug]/+page.svelte` — TripSummary (multi-day overview + map). Port from `projects/trips/frontend/src/pages/TripSummaryPage.jsx`.
- `[slug]/timeline/+page.svelte` — port `TripTimeline.jsx`.
- `[slug]/day/[day]/+page.svelte` — port `DayDetailPage.jsx` (per-day map, photo grid/viewer, elevation stats).
- `lib/trips/images.js` — imgproxy preset URL helper. Images are served by imgproxy on `img.jomcgi.dev` (Task 6), so URLs are ABSOLUTE and cross-origin (fine for `<img>`):
  ```js
  const IMG_BASE = "https://img.jomcgi.dev";
  const PRESETS = {
    thumb: "rs:fit:300:300/q:85",
    display: "rs:fit:1920:1080/q:92",
    preview: "rs:fit:1200:1200/q:90",
    gallery: "rs:fit:600:600/q:88",
  };
  export const imgUrl = (key, preset) =>
    `${IMG_BASE}/unsafe/${PRESETS[preset]}/plain/s3://monolith-trips/${key}`;
  export const fullUrl = (key) =>
    `${IMG_BASE}/unsafe/plain/s3://monolith-trips/${key}`;
  ```
- Port supporting components as Svelte: `TripMap`/`DayMap` (maplibre), `PhotoViewer`, `DayPhotoGrid`, `DayStatsCard`, `DayNavigation`, `TagFilter`, `ViewToggle`, elevation chart. Drop `LiveBadge` and any WS hook (no live path). Drop `useWeather` unless trivially portable.

**SSR data flow (match the other public apps):** the public frontend pod has `API_BASE=http://monolith-public-web:8000` (the public read-only backend, which now includes the trips `read_router` from Task 4). The `[slug]/+page.server.js` `load` fetches `${API_BASE}/api/trips/trip/${slug}` server-side and inlines `{trip, points}` into the SSR HTML. For any client-side data needs, add a same-origin `routes/public/app/trips/.../+server.js` proxy that calls `API_BASE` (the pattern used by `ships/heat/+server.js`, `stars/history/.../+server.js`, etc.), NOT a direct browser call. No public JSON API is exposed and no envoy HTTPRoute change is needed. Read an existing example like `routes/public/app/ships/+page.server.js` (or `hikes`) to copy the `load`/`API_BASE` idiom exactly.

**Portfolio links:** update the engineering/portfolio links that point at the old subdomain (`frontend/src/routes/public/engineering/engineering-data.js:287`, `diagrams/Trips.svelte:24`) to `jomcgi.dev/app/trips/...`.

**Tests:** add Vitest component tests where the standalone had them (`projects/trips/frontend/src/test/`), at minimum a render-without-crash + preset-URL-builder unit. Commit per page.

```bash
git add projects/monolith/frontend/src/routes/public/trips/ projects/monolith/frontend/src/lib/trips/
git commit -m "feat(trips): SvelteKit SSR trip summary/timeline/day pages"
```

---

## Task 8: Repoint the publish-trip-images tool from NATS to the ingestion endpoint

**Files:**

- Modify: `projects/trips/tools/publish-trip-images/main.py`

**Step 1:** Replace the three `await js.publish("trips.point", json.dumps(point).encode())` sites (lines ~773, 1455, 1665) and the JetStream setup (`get_jetstream`, line 728) with an HTTP POST that uploads the **image file** to `POST {TRIPS_INGEST_URL}/api/trips/ingest` with `X-Trips-Ingest-Key` and (when remote) Cloudflare Access headers `CF-Access-Client-Id` / `CF-Access-Client-Secret`. The endpoint now does EXIF server-side, so the tool no longer needs to extract optics/GPS before sending — simplify it to: dedupe via the local SQLite queue, then POST the raw JPEG with `trip`, `source`, `tags` params. Keep the SQLite persistent queue for **retry on non-2xx** (the tool's existing retry loop is the durability layer now that NATS is gone).

**Step 2:** Drop the `upload_image`→SeaweedFS path in the tool (the endpoint owns the S3 write now), or keep it only if you want client-side upload; simplest is endpoint-owns-everything. Remove `nats` from the tool's BUILD deps.

**Step 3:** `detect-wildlife` needs **no change** — it only captures to a local SQLite queue that `publish-trip-images` drains; it has no NATS/S3 call site.

**Step 4: Commit.**

```bash
git add projects/trips/tools/publish-trip-images/
git commit -m "feat(trips): repoint publish-trip-images from NATS to monolith ingestion endpoint"
```

---

## Task 9: Decommission the standalone trips service

Do this **only after** the monolith path is verified live (recovery done, pages render, an ingestion smoke-POST succeeds).

**Files:**

- Delete: `projects/trips/chart/`, `projects/trips/deploy/`, `projects/trips/backend/`, `projects/trips/frontend/` (frontend now in monolith)
- Keep: `projects/trips/tools/` (publish-trip-images, detect-wildlife, elevation lib) — these stay as operator CLIs. Consider relocating under `projects/monolith/trips/tools/` for cohesion (optional, separate PR).
- Modify: regenerate the ArgoCD root via `format` (runs `bazel/images/generate-home-cluster.sh`), removing the trips Application.
- Modify: `projects/trips/deploy/img-httpcheck-alert.yaml` removed; if you want imgproxy health monitoring, add an equivalent alert for the monolith imgproxy (see `add-httpcheck-alert` skill).

**Step 1:** Delete the standalone deploy + chart + backend + frontend. Run `format` to regenerate `projects/home-cluster/kustomization.yaml`.

**Step 2:** After merge + ArgoCD prune, confirm the `trips` namespace is gone and the legacy `trips` NATS subjects/stream are not recreated (they won't be — nothing publishes). Optionally delete the legacy `trips` S3 bucket once `monolith-trips` is confirmed complete (destructive; the bucket is the only remaining copy of original-resolution images if recovery copied rather than the inverse — verify object counts match first).

**Step 3: Commit.**

```bash
git add -A projects/trips projects/home-cluster
git commit -m "chore(trips): decommission standalone trips service (served by monolith)"
```

---

## End-of-plan verification (CI + operational)

1. Push the branch; `gh pr checks <number> --watch`. Read failures via `mcp__buildbuddy__get_invocation` (commitSha selector) → `get_target` → `get_log`. Quote the actual assertion before hypothesizing (per CLAUDE.md CI-diagnosis rule).
2. Likely CI touchpoints to pre-check: `main_public_imports_test` (closure clean), `public_reader_grants_test` (trips schema), `atlas.sum` freshness, semgrep on the new boto3 client (`# nosemgrep` + BUILD `exclude_rules`), and `bdd_completeness_test` if any new public callable lands in a domain `__init__.py` (`feedback_bdd_completeness_public_surface`).
3. After merge: run recovery (Task 5 step 4), confirm `jomcgi.dev/trips/2025-liard-hot-springs` renders with photos, then smoke-test one ingestion POST (local `port-forward`, then remote with the Cloudflare service token).
4. New GHCR image/chart packages may default **private** — flip to public if the chart pull 401s (per `project_monolith_modularity_program` gotcha).

## Open operational prerequisites (not code, do before/around merge)

- **Create the Cloudflare Access service token** and an Access policy allowing it on the ingestion path; store the client id/secret for the field device (and as a monolith secret if the app needs to validate beyond the X-key).
- **Provision `monolith-trips` bucket:** done declaratively in Task 6 step 0 via the seaweedfs chart's `s3.createBuckets` (GitOps, no manual step). The `trips/s3.py` autocreate-on-`NoSuchBucket` is only a fallback.
- **Locate any saved KMLs** for gap-route restoration; without them, gap points are not recoverable (accepted).
